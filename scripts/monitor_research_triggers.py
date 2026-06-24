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
from myinveststock.db import (
    TASK_TYPE_STOCK_RESEARCH,
    TRIGGER_PRICE_DEVIATION,
    TRIGGER_TRACKABLE_LEADER,
    connect,
    init_db,
    latest_report,
    list_latest_leaders,
    list_queue_for_stock,
    list_research_runs,
    upsert_queue_item,
    utc_now,
)
from myinveststock.leader_index import build_stock_research_prompt


def _load_json(value: object, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return fallback


def _num(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_stock_research(rows: list[object]) -> object | None:
    for row in rows:
        if row["task_type"] == TASK_TYPE_STOCK_RESEARCH:
            return row
    return None


def _has_active_stock_research_queue(rows: list[object]) -> bool:
    for row in rows:
        if row["task_type"] != TASK_TYPE_STOCK_RESEARCH:
            continue
        if row["status"] in {"pending", "in_progress"}:
            return True
    return False


def trigger_reason_for(
    *,
    leader: object,
    latest_run: object | None,
    price_deviation: float,
) -> str | None:
    if latest_run is None:
        return TRIGGER_TRACKABLE_LEADER
    market = _load_json(leader["market_json"], {})
    close = _num(market.get("close") if isinstance(market, dict) else None)
    low = _num(latest_run["valuation_low"])
    high = _num(latest_run["valuation_high"])
    if close is not None and low is not None and high is not None:
        if close < low * (1.0 - price_deviation) or close > high * (1.0 + price_deviation):
            return TRIGGER_PRICE_DEVIATION
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor single-stock research triggers and enqueue stock_research tasks.")
    parser.add_argument("--price-deviation", type=float, default=0.25, help="Deviation outside valuation range before requeue.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db(DB_PATH)
    queued: list[str] = []
    skipped: list[str] = []
    now = utc_now()
    with closing(connect(DB_PATH)) as conn:
        report = latest_report(conn)
        if report is None:
            print("no_latest_report")
            return 1
        report_dict = dict(report)
        leaders = list_latest_leaders(conn)
        for priority, leader in enumerate(leaders, start=1):
            runs = list_research_runs(conn, leader["code"])
            queue_rows = list_queue_for_stock(conn, leader["code"])
            if _has_active_stock_research_queue(queue_rows):
                skipped.append(f"{leader['code']} active_queue")
                continue
            latest_run = _latest_stock_research(runs)
            reason = trigger_reason_for(
                leader=leader,
                latest_run=latest_run,
                price_deviation=args.price_deviation,
            )
            if reason is None:
                skipped.append(f"{leader['code']} no_trigger")
                continue
            item = _load_json(leader["raw_json"], {})
            if not isinstance(item, dict):
                item = {"code": leader["code"], "name": leader["name"], "theme": leader["theme"]}
            if not args.dry_run:
                upsert_queue_item(
                    conn,
                    report_id=report["report_id"],
                    code=leader["code"],
                    name=leader["name"],
                    priority=priority,
                    stage=1,
                    task_type=TASK_TYPE_STOCK_RESEARCH,
                    task_keyword=f"MyInvestStock 个股深研 {leader['code']} {leader['name']}",
                    prompt=build_stock_research_prompt(item, report_dict, trigger_reason=reason),
                    depends_on_task_type=None,
                    trigger_reason=reason,
                    task_date=report["basis_date"],
                    now=now,
                )
            queued.append(f"{leader['code']} {leader['name']} {reason}")
        if not args.dry_run:
            conn.commit()

    print(f"queued_count={len(queued)}")
    for item in queued:
        print(f"queued {item}")
    print(f"skipped_count={len(skipped)}")
    for item in skipped:
        print(f"skipped {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
