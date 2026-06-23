from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from myinveststock.config import DB_PATH
from myinveststock.db import connect, init_db, insert_research_run, mark_queue_status


REQUIRED_FIELDS = {"code", "name", "task_type", "research_date"}
ALLOWED_TASK_TYPES = {"strategic", "financial", "combined"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import one single-stock research run from JSON.")
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--queue-status", default="complete", choices=["pending", "complete", "blocked"])
    args = parser.parse_args()

    run = json.loads(args.json_file.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED_FIELDS - set(run))
    if missing:
        print(f"missing required fields: {', '.join(missing)}")
        return 1
    if run["task_type"] not in ALLOWED_TASK_TYPES:
        print(f"invalid task_type: {run['task_type']}")
        return 1
    if run["task_type"] == "strategic" and any(run.get(key) is not None for key in ["valuation_low", "valuation_mid", "valuation_high"]):
        print("strategic research must not write valuation range")
        return 1
    init_db()
    with closing(connect(DB_PATH)) as conn:
        row_id = insert_research_run(conn, run)
        mark_queue_status(
            conn,
            code=run["code"],
            task_type=run["task_type"],
            status=args.queue_status,
            report_id=run.get("source_report_id"),
        )
        conn.commit()
    print(f"research_run_id={row_id}")
    print(f"code={run['code']}")
    print(f"task_type={run['task_type']}")
    print(f"queue_status={args.queue_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
