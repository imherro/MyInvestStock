from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from core.schema.stock_report import StockResearchReport
from core.task.state import TaskStatus, compute_task_id, compute_task_run_id, validate_transition

from .config import DB_PATH

REPORT_SCHEMA_VERSION = "stock_research_report.v1"
QUEUE_SOURCE_TRACKABLE = "trackable_leader"
QUEUE_SOURCE_REQUEST = "manual_request"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _create_research_queue_projection(conn: sqlite3.Connection, table_name: str = "research_queue") -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            priority INTEGER NOT NULL,
            stage INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            task_id TEXT,
            run_id TEXT,
            depends_on_task_type TEXT,
            source_type TEXT NOT NULL DEFAULT 'trackable_leader',
            source_detail TEXT,
            task_keyword TEXT NOT NULL,
            prompt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (report_id, code, task_type),
            FOREIGN KEY (report_id) REFERENCES leader_reports(report_id)
        )
        """
    )


def _migrate_research_queue_projection(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "research_queue")
    if "status" not in columns:
        _ensure_column(conn, "research_queue", "task_id", "TEXT")
        _ensure_column(conn, "research_queue", "run_id", "TEXT")
        _ensure_column(conn, "research_queue", "source_type", "TEXT NOT NULL DEFAULT 'trackable_leader'")
        _ensure_column(conn, "research_queue", "source_detail", "TEXT")
        return

    legacy_table = "research_queue_legacy_state"
    conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
    conn.execute(f"ALTER TABLE research_queue RENAME TO {legacy_table}")
    _create_research_queue_projection(conn)

    target_columns = [
        "id",
        "report_id",
        "code",
        "name",
        "priority",
        "stage",
        "task_type",
        "task_id",
        "run_id",
        "depends_on_task_type",
        "source_type",
        "source_detail",
        "task_keyword",
        "prompt",
        "created_at",
        "updated_at",
    ]
    legacy_columns = _table_columns(conn, legacy_table)
    select_exprs = []
    for column in target_columns:
        if column in legacy_columns:
            select_exprs.append(column)
        elif column == "source_type":
            select_exprs.append("'trackable_leader'")
        else:
            select_exprs.append("NULL")
    conn.execute(
        f"""
        INSERT INTO research_queue ({", ".join(target_columns)})
        SELECT {", ".join(select_exprs)}
        FROM {legacy_table}
        """
    )
    conn.execute(f"DROP TABLE {legacy_table}")


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

            CREATE TABLE IF NOT EXISTS stock_daily_prices (
                code TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                open_price REAL NOT NULL,
                high_price REAL NOT NULL,
                low_price REAL NOT NULL,
                close_price REAL NOT NULL,
                volume REAL,
                amount REAL,
                adj TEXT,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (code, trade_date)
            );

            CREATE TABLE IF NOT EXISTS task_queue (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                stock_code TEXT NOT NULL,
                task_type TEXT NOT NULL,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT NOT NULL,
                diff_metrics TEXT NOT NULL DEFAULT '{}',
                timestamp TEXT NOT NULL,
                UNIQUE (run_id, stage, input_hash, output_hash)
            );

            CREATE INDEX IF NOT EXISTS idx_trackable_code
                ON trackable_leaders(code);
            CREATE INDEX IF NOT EXISTS idx_task_queue_status
                ON task_queue(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_audit_log_run_stage
                ON audit_log(run_id, stage);
            CREATE INDEX IF NOT EXISTS idx_runs_code_date
                ON stock_research_runs(code, research_date);
            CREATE INDEX IF NOT EXISTS idx_daily_prices_code_date
                ON stock_daily_prices(code, trade_date);
            """
        )
        _create_research_queue_projection(conn)
        _migrate_research_queue_projection(conn)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_queue_run_id
                ON research_queue(run_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_queue_priority
                ON research_queue(priority, stage)
            """
        )
        conn.commit()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, sort_keys=True)


def queue_source_label(source_type: object) -> str:
    if source_type == QUEUE_SOURCE_TRACKABLE:
        return "可跟踪龙头"
    if source_type == QUEUE_SOURCE_REQUEST:
        return "其他请求"
    return str(source_type or "未知来源")


def normalize_trade_date(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return datetime.fromisoformat(text[:10]).date().isoformat()


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _required_float(value: object, field_name: str) -> float:
    number = _optional_float(value)
    if number is None:
        raise ValueError(f"missing numeric daily price field: {field_name}")
    return number


def upsert_daily_prices(
    conn: sqlite3.Connection,
    *,
    code: str,
    rows: Iterable[dict[str, Any]],
    source: str,
    adj: str | None = None,
    fetched_at: str | None = None,
) -> int:
    now = fetched_at or utc_now()
    written = 0
    for row in rows:
        trade_date = normalize_trade_date(row.get("trade_date"))
        values = (
            code,
            trade_date,
            _required_float(row.get("open"), "open"),
            _required_float(row.get("high"), "high"),
            _required_float(row.get("low"), "low"),
            _required_float(row.get("close"), "close"),
            _optional_float(row.get("vol") if "vol" in row else row.get("volume")),
            _optional_float(row.get("amount")),
            adj,
            source,
            now,
        )
        conn.execute(
            """
            INSERT INTO stock_daily_prices (
                code, trade_date, open_price, high_price, low_price, close_price,
                volume, amount, adj, source, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code, trade_date) DO UPDATE SET
                open_price=excluded.open_price,
                high_price=excluded.high_price,
                low_price=excluded.low_price,
                close_price=excluded.close_price,
                volume=excluded.volume,
                amount=excluded.amount,
                adj=excluded.adj,
                source=excluded.source,
                fetched_at=excluded.fetched_at
            """,
            values,
        )
        written += 1
    return written


def list_daily_prices(
    conn: sqlite3.Connection,
    code: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    clauses = ["code = ?"]
    params: list[Any] = [code]
    if start_date:
        clauses.append("trade_date >= ?")
        params.append(normalize_trade_date(start_date))
    if end_date:
        clauses.append("trade_date <= ?")
        params.append(normalize_trade_date(end_date))
    where = " AND ".join(clauses)
    columns = """
        code, trade_date, open_price, high_price, low_price, close_price,
        volume, amount, adj, source, fetched_at
    """
    if limit is not None:
        return list(
            conn.execute(
                f"""
                SELECT *
                FROM (
                    SELECT {columns}
                    FROM stock_daily_prices
                    WHERE {where}
                    ORDER BY trade_date DESC
                    LIMIT ?
                )
                ORDER BY trade_date ASC
                """,
                (*params, int(limit)),
            )
        )
    return list(
        conn.execute(
            f"""
            SELECT {columns}
            FROM stock_daily_prices
            WHERE {where}
            ORDER BY trade_date ASC
            """,
            params,
        )
    )


def task_status_to_queue_status(status: TaskStatus) -> str:
    if status == TaskStatus.RUNNING:
        return "in_progress"
    if status == TaskStatus.DONE:
        return "complete"
    if status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
        return "blocked"
    return "pending"


def queue_status_to_task_status(status: str) -> TaskStatus:
    mapping = {
        "pending": TaskStatus.PENDING,
        "in_progress": TaskStatus.RUNNING,
        "complete": TaskStatus.DONE,
        "blocked": TaskStatus.BLOCKED,
    }
    if status not in mapping:
        raise ValueError(f"unsupported queue status: {status}")
    return mapping[status]


def transition_task_status(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    target: TaskStatus,
    error_message: str | None = None,
) -> None:
    row = conn.execute(
        """
        SELECT status, retry_count
        FROM task_queue
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"missing task for run_id={run_id}")
    current = TaskStatus(row["status"])
    validate_transition(current, target)
    retry_count = int(row["retry_count"])
    if target == TaskStatus.RETRY and current == TaskStatus.FAILED:
        retry_count += 1
    conn.execute(
        """
        UPDATE task_queue
        SET status = ?, retry_count = ?, updated_at = ?, error_message = ?
        WHERE run_id = ?
        """,
        (target.value, retry_count, utc_now(), error_message, run_id),
    )


def idempotent_enqueue_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: str,
    stock_code: str,
    task_type: str,
    now: str,
) -> TaskStatus:
    row = conn.execute(
        """
        SELECT status
        FROM task_queue
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO task_queue (
                task_id, run_id, stock_code, task_type, status,
                retry_count, created_at, updated_at, error_message
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, NULL)
            """,
            (task_id, run_id, stock_code, task_type, TaskStatus.PENDING.value, now, now),
        )
        return TaskStatus.PENDING

    status = TaskStatus(row["status"])
    if status == TaskStatus.FAILED:
        transition_task_status(conn, run_id=run_id, target=TaskStatus.RETRY)
        transition_task_status(conn, run_id=run_id, target=TaskStatus.PENDING)
        return TaskStatus.PENDING
    if status == TaskStatus.RETRY:
        transition_task_status(conn, run_id=run_id, target=TaskStatus.PENDING)
        return TaskStatus.PENDING
    return status


def recover_stale_running_tasks(conn: sqlite3.Connection, *, stale_after_minutes: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    rows = list(
        conn.execute(
            """
            SELECT run_id, updated_at
            FROM task_queue
            WHERE status = ?
            """,
            (TaskStatus.RUNNING.value,),
        )
    )
    recovered = 0
    for row in rows:
        try:
            updated_at = datetime.fromisoformat(row["updated_at"])
        except ValueError:
            updated_at = datetime.min.replace(tzinfo=timezone.utc)
        if updated_at <= cutoff:
            transition_task_status(
                conn,
                run_id=row["run_id"],
                target=TaskStatus.FAILED,
                error_message=f"RUNNING exceeded {stale_after_minutes} minutes",
            )
            recovered += 1
    return recovered


def list_orphan_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT t.*
            FROM task_queue t
            LEFT JOIN research_queue q ON q.run_id = t.run_id
            WHERE q.run_id IS NULL
            ORDER BY t.created_at, t.run_id
            """
        )
    )


def get_task_status(conn: sqlite3.Connection, stock_code: str, task_type: str | None = None) -> list[sqlite3.Row]:
    if task_type:
        return list(
            conn.execute(
                """
                SELECT *
                FROM task_queue
                WHERE stock_code = ? AND task_type = ?
                ORDER BY updated_at DESC, run_id
                """,
                (stock_code, task_type),
            )
        )
    return list(
        conn.execute(
            """
            SELECT *
            FROM task_queue
            WHERE stock_code = ?
            ORDER BY updated_at DESC, task_type, run_id
            """,
            (stock_code,),
        )
    )


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
    task_date: str | None,
    now: str,
    source_type: str = QUEUE_SOURCE_TRACKABLE,
    source_detail: str | None = None,
) -> None:
    run_id = compute_task_run_id(code, task_type, task_date or report_id, REPORT_SCHEMA_VERSION)
    task_id = compute_task_id(run_id)
    idempotent_enqueue_task(
        conn,
        task_id=task_id,
        run_id=run_id,
        stock_code=code,
        task_type=task_type,
        now=now,
    )
    conn.execute(
        """
        INSERT INTO research_queue (
            report_id, code, name, priority, stage, task_type, task_id, run_id, depends_on_task_type,
            source_type, source_detail, task_keyword, prompt, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(report_id, code, task_type) DO UPDATE SET
            name=excluded.name,
            priority=excluded.priority,
            stage=excluded.stage,
            task_id=excluded.task_id,
            run_id=excluded.run_id,
            depends_on_task_type=excluded.depends_on_task_type,
            source_type=excluded.source_type,
            source_detail=excluded.source_detail,
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
            task_id,
            run_id,
            depends_on_task_type,
            source_type,
            source_detail,
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
        WHERE COALESCE(schema_version, '') != 'manual_research_request.v1'
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


def get_known_leader(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT l.*
        FROM trackable_leaders l
        JOIN leader_reports r ON r.report_id = l.report_id
        WHERE l.code = ?
        ORDER BY COALESCE(r.basis_date, r.generated_at, r.fetched_at) DESC, r.fetched_at DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()


def list_trackable_history(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                l.code,
                l.name,
                l.theme,
                l.deep_rating,
                l.deep_label,
                l.deep_score,
                l.candidate_leader_claim,
                r.report_id,
                r.basis_date,
                r.generated_at,
                r.fetched_at
            FROM trackable_leaders l
            JOIN leader_reports r ON r.report_id = l.report_id
            WHERE l.code = ?
            ORDER BY COALESCE(r.basis_date, r.generated_at, r.fetched_at) DESC, r.fetched_at DESC
            """,
            (code,),
        )
    )


def _queue_status_case() -> str:
    return """
        CASE t.status
            WHEN 'RUNNING' THEN 'in_progress'
            WHEN 'DONE' THEN 'complete'
            WHEN 'FAILED' THEN 'blocked'
            WHEN 'BLOCKED' THEN 'blocked'
            ELSE 'pending'
        END
    """


def list_queue(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    params: tuple[Any, ...]
    where = ""
    if status:
        if status == "blocked":
            where = "WHERE t.status IN (?, ?)"
            params = (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)
        else:
            where = "WHERE t.status = ?"
            params = (queue_status_to_task_status(status).value,)
    else:
        params = ()
    return list(
        conn.execute(
            f"""
            SELECT
                q.*,
                {_queue_status_case()} AS status,
                t.status AS task_status,
                t.retry_count AS retry_count,
                t.error_message AS error_message
            FROM research_queue q
            JOIN task_queue t ON t.run_id = q.run_id
            {where}
            ORDER BY q.priority ASC, q.stage ASC, q.id ASC
            """,
            params,
        )
    )


def list_queue_for_stock(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"""
            SELECT
                q.*,
                {_queue_status_case()} AS status,
                t.status AS task_status,
                t.retry_count AS retry_count,
                t.error_message AS error_message
            FROM research_queue q
            JOIN task_queue t ON t.run_id = q.run_id
            WHERE q.code = ?
            ORDER BY q.created_at DESC, q.priority ASC, q.stage ASC, q.id ASC
            """,
            (code,),
        )
    )


def next_queue_item(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT q.*
        FROM research_queue q
        JOIN task_queue t ON t.run_id = q.run_id
        WHERE t.status = 'PENDING'
          AND (
              q.depends_on_task_type IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM stock_research_runs r
                  WHERE r.code = q.code
                    AND r.task_type = q.depends_on_task_type
                    AND r.status = 'complete'
              )
          )
        ORDER BY q.priority ASC, q.stage ASC, q.id ASC
        LIMIT 1
        """
    ).fetchone()


def claim_next_queue_item(conn: sqlite3.Connection) -> sqlite3.Row | None:
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    recover_stale_running_tasks(conn)
    row = next_queue_item(conn)
    if row is None:
        conn.commit()
        return None
    transition_task_status(conn, run_id=row["run_id"], target=TaskStatus.RUNNING)
    conn.execute("UPDATE research_queue SET updated_at = ? WHERE id = ?", (now, row["id"]))
    conn.commit()
    return conn.execute(
        """
        SELECT *
        FROM research_queue
        WHERE id = ?
        """,
        (row["id"],),
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


def insert_research_run(conn: sqlite3.Connection, report: StockResearchReport) -> int:
    if not isinstance(report, StockResearchReport):
        run_id = report.get("run_id", "unknown") if isinstance(report, dict) else "unknown"
        stock_code = report.get("stock_code") or report.get("code", "unknown") if isinstance(report, dict) else "unknown"
        raise TypeError(
            "insert_research_run requires a validated StockResearchReport "
            f"(run_id={run_id}, stock_code={stock_code})"
        )
    now = utc_now()
    payload = report.model_dump(mode="json")
    fields = {
        "code": report.stock_code,
        "name": report.stock_name,
        "source_report_id": report.source_report_id,
        "task_type": report.task_type,
        "research_date": report.research_date,
        "created_at": now,
        "status": report.status,
        "title": report.title,
        "summary": report.summary,
        "valuation_low": report.valuation.intrinsic_value_low,
        "valuation_mid": report.valuation.intrinsic_value_mid,
        "valuation_high": report.valuation.intrinsic_value_high,
        "valuation_unit": report.valuation.unit,
        "valuation_method": report.valuation.method,
        "valuation_confidence": report.valuation.confidence,
        "industry_position": report.industry_position,
        "competition_landscape": report.competition_landscape,
        "upstream_downstream": report.upstream_downstream,
        "annual_growth": report.annual_growth,
        "multi_bagger_potential": report.multi_bagger_potential,
        "heavy_position_view": report.heavy_position_view,
        "evidence_json": _json(payload["evidence"]),
        "assumptions_json": _json(payload["assumptions"]),
        "risks_json": _json(report.risk.invalidation_conditions),
        "raw_json": _json(payload),
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
    if report_id:
        rows = list(
            conn.execute(
                """
                SELECT DISTINCT run_id
                FROM research_queue
                WHERE code = ? AND task_type = ? AND report_id = ? AND run_id IS NOT NULL
                """,
                (code, task_type, report_id),
            )
        )
    else:
        rows = list(
            conn.execute(
                """
                SELECT DISTINCT run_id
                FROM research_queue
                WHERE code = ? AND task_type = ? AND run_id IS NOT NULL
                """,
                (code, task_type),
            )
        )
    target = queue_status_to_task_status(status)
    for row in rows:
        transition_task_status(conn, run_id=row["run_id"], target=target)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
