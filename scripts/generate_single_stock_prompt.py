from __future__ import annotations

import argparse
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from myinveststock.config import DB_PATH
from myinveststock.db import connect, list_queue, next_queue_item


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one Codex prompt for one stock only.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--next", action="store_true", help="Use the next pending queue item.")
    group.add_argument("--code", help="Use a specific stock code from the queue.")
    parser.add_argument("--task-type", choices=["strategic", "financial"], help="Limit --code to one task type.")
    args = parser.parse_args()

    with closing(connect(DB_PATH)) as conn:
        if args.next:
            row = next_queue_item(conn)
        else:
            matches = [
                item
                for item in list_queue(conn)
                if item["code"].upper() == args.code.upper()
                and (args.task_type is None or item["task_type"] == args.task_type)
            ]
            row = matches[0] if matches else None
    if row is None:
        print("没有找到待研究股票。请先运行 python scripts/ingest_index.py。")
        return 1
    print(row["task_keyword"])
    print(f"task_type={row['task_type']}")
    print(f"depends_on_task_type={row['depends_on_task_type'] or ''}")
    print()
    print(row["prompt"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
