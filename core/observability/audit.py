from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any, Iterable

from .trace import TRACE_STAGES, TraceEvent


HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class DriftDetectionResult:
    run_id: str
    flags: list[str]
    checked_metrics: dict[str, float]


def _json(value: object) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def _loads(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def ensure_audit_store(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            output_hash TEXT NOT NULL,
            diff_metrics TEXT NOT NULL DEFAULT '{}',
            timestamp TEXT NOT NULL,
            UNIQUE (run_id, stage, input_hash, output_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_log_run_stage
            ON audit_log(run_id, stage)
        """
    )


def record_trace_events(conn: sqlite3.Connection, events: Iterable[TraceEvent]) -> int:
    ensure_audit_store(conn)
    inserted = 0
    for event in events:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO audit_log (
                run_id, stage, input_hash, output_hash, diff_metrics, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.run_id,
                event.stage,
                event.input_hash,
                event.output_hash,
                _json(event.diff_metrics),
                event.timestamp,
            ),
        )
        inserted += int(cursor.rowcount)
    return inserted


def audit_events_for_run(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    ensure_audit_store(conn)
    return list(
        conn.execute(
            """
            SELECT *
            FROM audit_log
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        )
    )


def verify_run(conn: sqlite3.Connection, run_id: str, expected_report_hash: str | None = None) -> bool:
    rows = audit_events_for_run(conn, run_id)
    if not rows:
        return False

    by_stage: dict[str, list[sqlite3.Row]] = {stage: [] for stage in TRACE_STAGES}
    for row in rows:
        if row["stage"] in by_stage:
            by_stage[row["stage"]].append(row)
        if not HASH_RE.match(row["input_hash"]) or not HASH_RE.match(row["output_hash"]):
            return False

    if any(not by_stage[stage] for stage in TRACE_STAGES):
        return False

    for stage, stage_rows in by_stage.items():
        input_hashes = {row["input_hash"] for row in stage_rows}
        output_hashes = {row["output_hash"] for row in stage_rows}
        if len(input_hashes) > 1 or len(output_hashes) > 1:
            return False

    if expected_report_hash is not None:
        report_metrics = [_loads(row["diff_metrics"]) for row in by_stage["report"]]
        if not any(metrics.get("report_hash") == expected_report_hash for metrics in report_metrics):
            return False
    return True


def _report_metrics(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    rows = audit_events_for_run(conn, run_id)
    for row in rows:
        if row["stage"] == "report":
            return _loads(row["diff_metrics"])
    return {}


def _prior_report_values(conn: sqlite3.Connection, run_id: str, metric: str) -> list[float]:
    ensure_audit_store(conn)
    values: list[float] = []
    rows = conn.execute(
        """
        SELECT diff_metrics
        FROM audit_log
        WHERE stage = 'report'
          AND run_id <> ?
        ORDER BY id DESC
        LIMIT 50
        """,
        (run_id,),
    )
    for row in rows:
        metrics = _loads(row["diff_metrics"])
        try:
            values.append(float(metrics[metric]))
        except (KeyError, TypeError, ValueError):
            continue
    return values


def detect_basic_drift(conn: sqlite3.Connection, run_id: str) -> DriftDetectionResult:
    current = _report_metrics(conn, run_id)
    checked: dict[str, float] = {}
    flags: list[str] = []

    for metric in ["pe", "pb", "undervalued_score", "risk_adjusted_score"]:
        try:
            checked[metric] = float(current[metric])
        except (KeyError, TypeError, ValueError):
            continue

    pe = checked.get("pe")
    pb = checked.get("pb")
    if pe is not None and (pe <= 0.0 or pe >= 80.0):
        flags.append("pe_extreme")
    if pb is not None and (pb < 0.0 or pb >= 20.0):
        flags.append("pb_extreme")

    for metric in ["undervalued_score", "risk_adjusted_score"]:
        if metric not in checked:
            continue
        prior = _prior_report_values(conn, run_id, metric)
        if len(prior) < 5:
            continue
        baseline = mean(prior)
        spread = pstdev(prior) if len(prior) > 1 else 0.0
        threshold = max(25.0, spread * 2.0)
        if abs(checked[metric] - baseline) > threshold:
            flags.append(f"{metric}_distribution_shift")

    return DriftDetectionResult(run_id=run_id, flags=flags, checked_metrics=checked)
