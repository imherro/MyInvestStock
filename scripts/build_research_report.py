from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.report import build_stock_report
from core.observability import TraceRecorder, record_trace_events
from myinveststock.db import connect, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one deterministic StockResearchReport from structured inputs.")
    parser.add_argument("input_file", type=Path)
    parser.add_argument("--audit-db", type=Path, help="Optional SQLite DB path for writing audit_log trace events.")
    args = parser.parse_args()

    input_data: Any = json.loads(args.input_file.read_text(encoding="utf-8"))
    recorder = TraceRecorder() if args.audit_db else None
    report = build_stock_report(input_data, trace_recorder=recorder)
    if args.audit_db and recorder is not None:
        init_db(args.audit_db)
        with connect(args.audit_db) as conn:
            record_trace_events(conn, recorder.events)
            conn.commit()
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
