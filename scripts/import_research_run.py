from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.schema.stock_report import StockResearchReport, validate_stock_research_report
from myinveststock.config import DB_PATH
from myinveststock.db import connect, init_db, insert_research_run, mark_queue_status


def validation_context(raw_output: object) -> tuple[str, str]:
    if not isinstance(raw_output, dict):
        return "unknown", "unknown"
    run_id = str(raw_output.get("run_id") or "unknown")
    stock_code = str(raw_output.get("stock_code") or raw_output.get("code") or "unknown")
    return run_id, stock_code


def load_validated_report(json_file: Path) -> StockResearchReport:
    raw_output: Any = json.loads(json_file.read_text(encoding="utf-8"))
    try:
        return validate_stock_research_report(raw_output)
    except Exception as exc:
        run_id, stock_code = validation_context(raw_output)
        print(f"validation_failed run_id={run_id} stock_code={stock_code}", file=sys.stderr)
        raise exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Import one single-stock research run from JSON.")
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--queue-status", default="complete", choices=["pending", "complete", "blocked"])
    args = parser.parse_args()

    report = load_validated_report(args.json_file)
    init_db()
    with closing(connect(DB_PATH)) as conn:
        row_id = insert_research_run(conn, report)
        mark_queue_status(
            conn,
            code=report.stock_code,
            task_type=report.task_type,
            status=args.queue_status,
            report_id=report.source_report_id,
        )
        conn.commit()
    print(f"research_run_id={row_id}")
    print(f"run_id={report.run_id}")
    print(f"code={report.stock_code}")
    print(f"task_type={report.task_type}")
    print(f"queue_status={args.queue_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
