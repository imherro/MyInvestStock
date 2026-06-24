from __future__ import annotations

import tempfile
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path

from core.task.state import TaskStatus, compute_task_run_id, validate_transition
from myinveststock.db import (
    QUEUE_SOURCE_TRACKABLE,
    TASK_TYPE_STOCK_RESEARCH,
    TRIGGER_TRACKABLE_LEADER,
    claim_next_queue_item,
    connect,
    init_db,
    get_task_status,
    list_queue,
    list_orphan_tasks,
    mark_queue_status,
    recover_stale_running_tasks,
    transition_task_status,
    upsert_queue_item,
    upsert_report,
)


def add_report(conn) -> None:
    upsert_report(
        conn,
        report_id="leader_review_2026-06-24",
        schema_version="leader.v1",
        generated_at="2026-06-24T18:50:00+08:00",
        basis_date="2026-06-24",
        theme_report_id=None,
        source_url="https://leader.okbbc.com/api/index",
        fetched_at="2026-06-24T11:00:00+00:00",
        raw_path=None,
    )


def enqueue_stock_research(conn) -> str:
    upsert_queue_item(
        conn,
        report_id="leader_review_2026-06-24",
        code="600519.SH",
        name="贵州茅台",
        priority=1,
        stage=1,
        task_type=TASK_TYPE_STOCK_RESEARCH,
        task_keyword="MyInvestStock 个股深研 600519.SH 贵州茅台",
        prompt="研究提示词",
        depends_on_task_type=None,
        trigger_reason=TRIGGER_TRACKABLE_LEADER,
        task_date="2026-06-24",
        now="2026-06-24T11:00:00+00:00",
    )
    return compute_task_run_id("600519.SH", TASK_TYPE_STOCK_RESEARCH, "2026-06-24", "stock_research_report.v1")


class TaskStateTests(unittest.TestCase):
    def test_init_db_migrates_existing_research_queue_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "migration.sqlite"
            conn = connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE research_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        report_id TEXT NOT NULL,
                        code TEXT NOT NULL,
                        name TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        stage INTEGER NOT NULL,
                        task_type TEXT NOT NULL,
                        depends_on_task_type TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        task_keyword TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (report_id, code, task_type)
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            init_db(db_path)
            with closing(connect(db_path)) as migrated:
                columns = {
                    row["name"]
                    for row in migrated.execute("PRAGMA table_info(research_queue)")
                }
                index = migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_queue_run_id'"
                ).fetchone()
            self.assertIn("task_id", columns)
            self.assertIn("run_id", columns)
            self.assertIn("trigger_reason", columns)
            self.assertIn("source_type", columns)
            self.assertIn("source_detail", columns)
            self.assertNotIn("status", columns)
            self.assertIsNotNone(index)

    def test_state_transition_rules_reject_skips(self) -> None:
        validate_transition(TaskStatus.PENDING, TaskStatus.RUNNING)
        validate_transition(TaskStatus.RUNNING, TaskStatus.DONE)
        with self.assertRaises(ValueError):
            validate_transition(TaskStatus.PENDING, TaskStatus.DONE)

    def test_idempotent_enqueue_claim_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                add_report(conn)
                run_id = enqueue_stock_research(conn)
                enqueue_stock_research(conn)
                conn.commit()
                task_count = conn.execute("SELECT COUNT(*) AS count FROM task_queue").fetchone()["count"]
                self.assertEqual(task_count, 1)

                claimed = claim_next_queue_item(conn)
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed["run_id"], run_id)
                task = conn.execute("SELECT status FROM task_queue WHERE run_id = ?", (run_id,)).fetchone()
                self.assertEqual(task["status"], TaskStatus.RUNNING.value)
                queue_view = list_queue(conn)
                self.assertEqual(queue_view[0]["status"], "in_progress")
                self.assertEqual(queue_view[0]["source_type"], QUEUE_SOURCE_TRACKABLE)
                self.assertEqual(queue_view[0]["trigger_reason"], TRIGGER_TRACKABLE_LEADER)
                self.assertEqual(get_task_status(conn, "600519.SH", TASK_TYPE_STOCK_RESEARCH)[0]["status"], TaskStatus.RUNNING.value)

                mark_queue_status(
                    conn,
                    code="600519.SH",
                    task_type=TASK_TYPE_STOCK_RESEARCH,
                    status="complete",
                    report_id="leader_review_2026-06-24",
                )
                conn.commit()
                done = conn.execute(
                    "SELECT status, retry_count FROM task_queue WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                self.assertEqual(done["status"], TaskStatus.DONE.value)
                self.assertEqual(done["retry_count"], 0)
                self.assertEqual(list_queue(conn)[0]["status"], "complete")

                enqueue_stock_research(conn)
                still_done = conn.execute("SELECT status FROM task_queue WHERE run_id = ?", (run_id,)).fetchone()
                self.assertEqual(still_done["status"], TaskStatus.DONE.value)
                self.assertEqual(list_orphan_tasks(conn), [])
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE research_queue SET status = 'pending'")

    def test_failed_task_reenqueue_becomes_pending_with_retry_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "retry.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                add_report(conn)
                run_id = enqueue_stock_research(conn)
                conn.commit()
                self.assertIsNotNone(claim_next_queue_item(conn))
                transition_task_status(conn, run_id=run_id, target=TaskStatus.FAILED, error_message="validation error")
                enqueue_stock_research(conn)
                row = conn.execute(
                    "SELECT status, retry_count FROM task_queue WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                self.assertEqual(row["status"], TaskStatus.PENDING.value)
                self.assertEqual(row["retry_count"], 1)

    def test_stale_running_task_recovers_to_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "stale.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                add_report(conn)
                run_id = enqueue_stock_research(conn)
                conn.commit()
                self.assertIsNotNone(claim_next_queue_item(conn))
                conn.execute(
                    "UPDATE task_queue SET updated_at = ? WHERE run_id = ?",
                    ("2000-01-01T00:00:00+00:00", run_id),
                )
                recovered = recover_stale_running_tasks(conn, stale_after_minutes=30)
                row = conn.execute(
                    "SELECT status, error_message FROM task_queue WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                self.assertEqual(recovered, 1)
                self.assertEqual(row["status"], TaskStatus.FAILED.value)
                self.assertIn("RUNNING exceeded", row["error_message"])
                queue_row = list_queue(conn)[0]
                self.assertEqual(queue_row["status"], "blocked")
                self.assertEqual(queue_row["task_status"], TaskStatus.FAILED.value)


if __name__ == "__main__":
    unittest.main()
