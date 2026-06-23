from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    with closing(connect(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS leader_reports (
                report_id TEXT PRIMARY KEY,
                schema_version TEXT,
                generated_at TEXT,
                basis_date TEXT,
                theme_report_id TEXT,
                source_url TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                raw_path TEXT
            );

            CREATE TABLE IF NOT EXISTS trackable_leaders (
                report_id TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                xueqiu_url TEXT,
                theme TEXT,
                themes_json TEXT,
                deep_rating TEXT,
                deep_label TEXT,
                deep_score REAL,
                shadow_observation_eligible INTEGER,
                candidate_leader_tier TEXT,
                candidate_leader_claim TEXT,
                candidate_evidence_score REAL,
                candidate_evidence_count INTEGER,
                candidate_hard_evidence_count INTEGER,
                market_json TEXT,
                scores_json TEXT,
                risk_flags_json TEXT,
                data_gaps_json TEXT,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (report_id, code),
                FOREIGN KEY (report_id) REFERENCES leader_reports(report_id)
            );

            CREATE TABLE IF NOT EXISTS research_queue (
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
                UNIQUE (report_id, code, task_type),
                FOREIGN KEY (report_id) REFERENCES leader_reports(report_id)
            );

            CREATE TABLE IF NOT EXISTS stock_research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                source_report_id TEXT,
                task_type TEXT NOT NULL DEFAULT 'combined',
                research_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT,
                summary TEXT,
                valuation_low REAL,
                valuation_mid REAL,
                valuation_high REAL,
                valuation_unit TEXT NOT NULL DEFAULT 'CNY/share',
                valuation_method TEXT,
                valuation_confidence TEXT,
                industry_position TEXT,
                competition_landscape TEXT,
                upstream_downstream TEXT,
                annual_growth TEXT,
                multi_bagger_potential TEXT,
                heavy_position_view TEXT,
                evidence_json TEXT,
                assumptions_json TEXT,
                risks_json TEXT,
                raw_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trackable_code
                ON trackable_leaders(code);
            CREATE INDEX IF NOT EXISTS idx_queue_status
                ON research_queue(status, priority);
            CREATE INDEX IF NOT EXISTS idx_runs_code_date
                ON stock_research_runs(code, research_date);
            """
        )
        conn.commit()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, sort_keys=True)


def upsert_report(
    conn: sqlite3.Connection,
    *,
    report_id: str,
    schema_version: str | None,
    generated_at: str | None,
    basis_date: str | None,
    theme_report_id: str | None,
    source_url: str,
    fetched_at: str,
    raw_path: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO leader_reports (
            report_id, schema_version, generated_at, basis_date,
            theme_report_id, source_url, fetched_at, raw_path
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(report_id) DO UPDATE SET
            schema_version=excluded.schema_version,
            generated_at=excluded.generated_at,
            basis_date=excluded.basis_date,
            theme_report_id=excluded.theme_report_id,
            source_url=excluded.source_url,
            fetched_at=excluded.fetched_at,
            raw_path=excluded.raw_path
        """,
        (
            report_id,
            schema_version,
            generated_at,
            basis_date,
            theme_report_id,
            source_url,
            fetched_at,
            raw_path,
        ),
    )


def upsert_trackable_leader(
    conn: sqlite3.Connection,
    *,
    report_id: str,
    item: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO trackable_leaders (
            report_id, code, name, xueqiu_url, theme, themes_json,
            deep_rating, deep_label, deep_score, shadow_observation_eligible,
            candidate_leader_tier, candidate_leader_claim, candidate_evidence_score,
            candidate_evidence_count, candidate_hard_evidence_count,
            market_json, scores_json, risk_flags_json, data_gaps_json,
            raw_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(report_id, code) DO UPDATE SET
            name=excluded.name,
            xueqiu_url=excluded.xueqiu_url,
            theme=excluded.theme,
            themes_json=excluded.themes_json,
            deep_rating=excluded.deep_rating,
            deep_label=excluded.deep_label,
            deep_score=excluded.deep_score,
            shadow_observation_eligible=excluded.shadow_observation_eligible,
            candidate_leader_tier=excluded.candidate_leader_tier,
            candidate_leader_claim=excluded.candidate_leader_claim,
            candidate_evidence_score=excluded.candidate_evidence_score,
            candidate_evidence_count=excluded.candidate_evidence_count,
            candidate_hard_evidence_count=excluded.candidate_hard_evidence_count,
            market_json=excluded.market_json,
            scores_json=excluded.scores_json,
            risk_flags_json=excluded.risk_flags_json,
            data_gaps_json=excluded.data_gaps_json,
            raw_json=excluded.raw_json
        """,
        (
            report_id,
            item["code"],
            item["name"],
            item.get("xueqiu_url"),
            item.get("theme"),
            _json(item.get("themes")),
            item.get("deep_rating"),
            item.get("deep_label"),
            item.get("deep_score"),
            1 if item.get("shadow_observation_eligible") else 0,
            item.get("candidate_leader_tier"),
            item.get("candidate_leader_claim"),
            item.get("candidate_evidence_score"),
            item.get("candidate_evidence_count"),
            item.get("candidate_hard_evidence_count"),
            _json(item.get("market")),
            _json(item.get("scores")),
            _json(item.get("risk_flags")),
            _json(item.get("data_gaps")),
            _json(item),
            created_at,
        ),
    )


def upsert_queue_item(
    conn: sqlite3.Connection,
    *,
    report_id: str,
    code: str,
    name: str,
    priority: int,
    stage: int,
    task_type: str,
    task_keyword: str,
    prompt: str,
    depends_on_task_type: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO research_queue (
            report_id, code, name, priority, stage, task_type, depends_on_task_type,
            status, task_keyword, prompt, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        ON CONFLICT(report_id, code, task_type) DO UPDATE SET
            name=excluded.name,
            priority=excluded.priority,
            stage=excluded.stage,
            depends_on_task_type=excluded.depends_on_task_type,
            task_keyword=excluded.task_keyword,
            prompt=excluded.prompt,
            updated_at=excluded.updated_at
        """,
        (
            report_id,
            code,
            name,
            priority,
            stage,
            task_type,
            depends_on_task_type,
            task_keyword,
            prompt,
            now,
            now,
        ),
    )


def latest_report(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM leader_reports
        ORDER BY COALESCE(generated_at, fetched_at) DESC, fetched_at DESC
        LIMIT 1
        """
    ).fetchone()


def list_latest_leaders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    report = latest_report(conn)
    if report is None:
        return []
    return list(
        conn.execute(
            """
            SELECT *
            FROM trackable_leaders
            WHERE report_id = ?
            ORDER BY deep_score DESC, code
            """,
            (report["report_id"],),
        )
    )


def get_latest_leader(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    report = latest_report(conn)
    if report is None:
        return None
    return conn.execute(
        """
        SELECT *
        FROM trackable_leaders
        WHERE report_id = ? AND code = ?
        """,
        (report["report_id"], code),
    ).fetchone()


def list_queue(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    params: tuple[Any, ...]
    where = ""
    if status:
        where = "WHERE status = ?"
        params = (status,)
    else:
        params = ()
    return list(
        conn.execute(
            f"""
            SELECT *
            FROM research_queue
            {where}
            ORDER BY priority ASC, stage ASC, id ASC
            """,
            params,
        )
    )


def next_queue_item(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM research_queue
        WHERE status = 'pending'
        ORDER BY priority ASC, stage ASC, id ASC
        LIMIT 1
        """
    ).fetchone()


def has_strategic_work(conn: sqlite3.Connection, code: str) -> bool:
    queued = conn.execute(
        """
        SELECT 1
        FROM research_queue
        WHERE code = ? AND task_type = 'strategic'
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    if queued:
        return True
    completed = conn.execute(
        """
        SELECT 1
        FROM stock_research_runs
        WHERE code = ? AND task_type = 'strategic'
        LIMIT 1
        """,
        (code,),
    ).fetchone()
    return completed is not None


def list_research_runs(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM stock_research_runs
            WHERE code = ?
            ORDER BY research_date DESC, id DESC
            """,
            (code,),
        )
    )


def valuation_runs(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM stock_research_runs
            WHERE code = ?
              AND valuation_low IS NOT NULL
              AND valuation_mid IS NOT NULL
              AND valuation_high IS NOT NULL
            ORDER BY research_date ASC, id ASC
            """,
            (code,),
        )
    )


def insert_research_run(conn: sqlite3.Connection, run: dict[str, Any]) -> int:
    now = utc_now()
    fields = {
        "code": run["code"],
        "name": run["name"],
        "source_report_id": run.get("source_report_id"),
        "task_type": run.get("task_type") or "combined",
        "research_date": run["research_date"],
        "created_at": run.get("created_at") or now,
        "status": run.get("status") or "complete",
        "title": run.get("title"),
        "summary": run.get("summary"),
        "valuation_low": run.get("valuation_low"),
        "valuation_mid": run.get("valuation_mid"),
        "valuation_high": run.get("valuation_high"),
        "valuation_unit": run.get("valuation_unit") or "CNY/share",
        "valuation_method": run.get("valuation_method"),
        "valuation_confidence": run.get("valuation_confidence"),
        "industry_position": run.get("industry_position"),
        "competition_landscape": run.get("competition_landscape"),
        "upstream_downstream": run.get("upstream_downstream"),
        "annual_growth": run.get("annual_growth"),
        "multi_bagger_potential": run.get("multi_bagger_potential"),
        "heavy_position_view": run.get("heavy_position_view"),
        "evidence_json": _json(run.get("evidence")),
        "assumptions_json": _json(run.get("assumptions")),
        "risks_json": _json(run.get("risks")),
        "raw_json": _json(run),
    }
    columns = ", ".join(fields.keys())
    placeholders = ", ".join("?" for _ in fields)
    cursor = conn.execute(
        f"INSERT INTO stock_research_runs ({columns}) VALUES ({placeholders})",
        tuple(fields.values()),
    )
    return int(cursor.lastrowid)


def mark_queue_status(
    conn: sqlite3.Connection,
    *,
    code: str,
    task_type: str,
    status: str,
    report_id: str | None = None,
) -> None:
    now = utc_now()
    if report_id:
        conn.execute(
            """
            UPDATE research_queue
            SET status = ?, updated_at = ?
            WHERE code = ? AND task_type = ? AND report_id = ?
            """,
            (status, now, code, task_type, report_id),
        )
    else:
        conn.execute(
            """
            UPDATE research_queue
            SET status = ?, updated_at = ?
            WHERE code = ? AND task_type = ?
            """,
            (status, now, code, task_type),
        )


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
