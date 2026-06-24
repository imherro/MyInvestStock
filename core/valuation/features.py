from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class FundamentalFeatures:
    revenue_growth_3y: float
    profit_growth_3y: float
    roe_avg: float
    gross_margin: float
    debt_to_equity: float
    fcf_yield: float


def _safe_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _average(values: Sequence[float]) -> float:
    clean = [value for value in values if value == value]
    if not clean:
        return 0.0
    return sum(clean) / len(clean)


def _cagr(start: float, end: float, periods: int) -> float:
    if start <= 0 or end <= 0 or periods <= 0:
        return 0.0
    return (end / start) ** (1.0 / periods) - 1.0


def _gross_margin(row: Mapping[str, object]) -> float:
    if row.get("gross_margin") is not None:
        return _safe_float(row.get("gross_margin"))
    revenue = _safe_float(row.get("revenue"))
    gross_profit = _safe_float(row.get("gross_profit"))
    if revenue <= 0:
        return 0.0
    return gross_profit / revenue


def _roe(row: Mapping[str, object]) -> float:
    if row.get("roe") is not None:
        return _safe_float(row.get("roe"))
    equity = _safe_float(row.get("equity") or row.get("total_equity"))
    profit = _safe_float(row.get("net_profit"))
    if equity <= 0:
        return 0.0
    return profit / equity


def extract_fundamental_features(rows: Sequence[Mapping[str, object]]) -> FundamentalFeatures:
    ordered = list(rows)
    if not ordered:
        return FundamentalFeatures(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    first = ordered[0]
    last = ordered[-1]
    periods = max(1, min(3, len(ordered) - 1))

    revenue_growth = _cagr(_safe_float(first.get("revenue")), _safe_float(last.get("revenue")), periods)
    profit_growth = _cagr(_safe_float(first.get("net_profit")), _safe_float(last.get("net_profit")), periods)
    roe_avg = _average([_roe(row) for row in ordered[-4:]])
    gross_margin = _average([_gross_margin(row) for row in ordered[-4:]])

    latest_equity = _safe_float(last.get("equity") or last.get("total_equity"))
    latest_debt = _safe_float(last.get("debt") or last.get("total_debt"))
    debt_to_equity = latest_debt / latest_equity if latest_equity > 0 else 0.0

    latest_fcf = _safe_float(last.get("free_cash_flow") or last.get("fcf"))
    latest_market_cap = _safe_float(last.get("market_cap"))
    fcf_yield = latest_fcf / latest_market_cap if latest_market_cap > 0 else 0.0

    return FundamentalFeatures(
        revenue_growth_3y=revenue_growth,
        profit_growth_3y=profit_growth,
        roe_avg=roe_avg,
        gross_margin=gross_margin,
        debt_to_equity=debt_to_equity,
        fcf_yield=fcf_yield,
    )
