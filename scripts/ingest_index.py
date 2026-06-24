from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from myinveststock.config import LEADER_INDEX_URL, THEME_INDEX_URL
from myinveststock.leader_index import fetch_index, ingest_payload, report_meta, save_raw_payload
from myinveststock.theme_index import fetch_theme_index, save_theme_payload, theme_report_meta


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest A trackable leaders from /api/index.")
    parser.add_argument("--url", default=LEADER_INDEX_URL)
    parser.add_argument("--theme-url", default=THEME_INDEX_URL)
    parser.add_argument("--skip-theme", action="store_true")
    parser.add_argument("--skip-raw", action="store_true")
    args = parser.parse_args()

    payload = fetch_index(args.url)
    meta = report_meta(payload)
    theme_payload = None
    theme_meta = None
    raw_path = None
    if not args.skip_raw:
        raw_path = str(save_raw_payload(payload, meta["report_id"]))
    if not args.skip_theme:
        theme_payload = fetch_theme_index(args.theme_url)
        theme_meta = theme_report_meta(theme_payload)
        if not args.skip_raw:
            save_theme_payload(theme_payload, theme_meta["report_id"])
    result = ingest_payload(payload, source_url=args.url, raw_path=raw_path, theme_payload=theme_payload)
    print(f"report_id={result['report_id']}")
    print(f"theme_report_id={result.get('theme_report_id') or (theme_meta or {}).get('report_id') or 'none'}")
    print(f"basis_date={result['basis_date']}")
    print(f"trackable_count={result['count']}")
    for code, name in zip(result["codes"], result["names"]):
        print(f"{code} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
