from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from myinveststock.config import DB_PATH
from myinveststock.db import connect, init_db, list_latest_leaders, list_price_refresh_subjects, upsert_daily_prices

DEFAULT_PRICE_START_DATE = "2024-09-24"


def load_env_token() -> str | None:
    token = os.environ.get("TUSHARE_TOKEN")
    if token:
        return token.strip()
    env_path = ROOT / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        if key.strip() == "TUSHARE_TOKEN":
            return value.strip().strip('"').strip("'") or None
    return None


def compact_date(value: str) -> str:
    return value.replace("-", "")


def date_days_ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).date().isoformat()


def dataframe_records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", True):
        return []
    records = frame.to_dict(orient="records")
    records.sort(key=lambda row: str(row.get("trade_date", "")))
    return records


def fetch_tushare_daily(ts: Any, code: str, start_date: str, end_date: str, adj: str) -> list[dict[str, Any]]:
    frame = ts.pro_bar(
        ts_code=code,
        adj=None if adj == "none" else adj,
        start_date=compact_date(start_date),
        end_date=compact_date(end_date),
    )
    return dataframe_records(frame)


def tracked_codes() -> list[tuple[str, str]]:
    init_db(DB_PATH)
    with connect(DB_PATH) as conn:
        return [(row["code"], row["name"]) for row in list_latest_leaders(conn)]


def system_codes() -> list[tuple[str, str]]:
    init_db(DB_PATH)
    with connect(DB_PATH) as conn:
        return [(row["code"], row["name"]) for row in list_price_refresh_subjects(conn)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh local A-share daily price cache for stock chart overlays.")
    parser.add_argument("--code", action="append", help="Stock code such as 600519.SH. Can be repeated.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tracked", action="store_true", help="Refresh only the latest /api/index A trackable leaders.")
    mode.add_argument(
        "--all-system",
        action="store_true",
        help="Refresh every stock already present in leader history, research queue, or research runs.",
    )
    parser.add_argument(
        "--days",
        type=int,
        help="Calendar-day lookback when --start-date is omitted. Defaults to the 2024-09-24 bull-market start.",
    )
    parser.add_argument("--start-date", help="Inclusive start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", default=datetime.now().date().isoformat(), help="Inclusive end date, YYYY-MM-DD.")
    parser.add_argument("--adj", choices=["qfq", "hfq", "none"], default="qfq", help="Tushare adjustment mode.")
    args = parser.parse_args()

    codes: list[tuple[str, str]] = []
    if args.tracked:
        codes.extend(tracked_codes())
    elif args.all_system or not args.code:
        codes.extend(system_codes())
    if args.code:
        known = {code for code, _ in codes}
        codes.extend((code, "") for code in args.code if code not in known)
    if not codes:
        print("no_codes=1")
        return 0

    token = load_env_token()
    if not token:
        print("missing_tushare_token=1")
        return 2

    try:
        import tushare as ts
    except ImportError:
        print("missing_python_package=tushare")
        return 2

    ts.set_token(token)
    start_date = args.start_date or (date_days_ago(args.days) if args.days else DEFAULT_PRICE_START_DATE)

    init_db(DB_PATH)
    with connect(DB_PATH) as conn:
        for code, name in codes:
            records = fetch_tushare_daily(ts, code, start_date, args.end_date, args.adj)
            written = upsert_daily_prices(
                conn,
                code=code,
                rows=records,
                source="Tushare pro_bar",
                adj=args.adj,
            )
            print(f"{code} {name} price_rows={written}")
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
