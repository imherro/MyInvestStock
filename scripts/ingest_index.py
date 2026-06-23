from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from myinveststock.config import LEADER_INDEX_URL
from myinveststock.leader_index import fetch_index, ingest_payload, report_meta, save_raw_payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest A trackable leaders from /api/index.")
    parser.add_argument("--url", default=LEADER_INDEX_URL)
    parser.add_argument("--skip-raw", action="store_true")
    args = parser.parse_args()

    payload = fetch_index(args.url)
    meta = report_meta(payload)
    raw_path = None
    if not args.skip_raw:
        raw_path = str(save_raw_payload(payload, meta["report_id"]))
    result = ingest_payload(payload, source_url=args.url, raw_path=raw_path)
    print(f"report_id={result['report_id']}")
    print(f"basis_date={result['basis_date']}")
    print(f"trackable_count={result['count']}")
    for code, name in zip(result["codes"], result["names"]):
        print(f"{code} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
