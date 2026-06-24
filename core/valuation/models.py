from __future__ import annotations

from dataclasses import dataclass


PE_CLIP_RANGE = (5.0, 50.0)
PB_CLIP_RANGE = (0.5, 15.0)
DEFAULT_ROE_BASELINE = 0.12
DCF_DISCOUNT_RATE_LOW = 0.08
DCF_DISCOUNT_RATE_MID = 0.10
DCF_DISCOUNT_RATE_HIGH = 0.12
DCF_TERMINAL_GROWTH = 0.03
VALUATION_BAND_WIDTH = 0.20


@dataclass(frozen=True)
class IntrinsicValueRange:
    low: float
    mid: float
    high: float
    method: str


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _range_from_mid(mid: float, method: str, band_width: float = VALUATION_BAND_WIDTH) -> IntrinsicValueRange:
    low = max(0.0, mid * (1.0 - band_width))
    high = max(low, mid * (1.0 + band_width))
    return IntrinsicValueRange(low=low, mid=mid, high=high, method=method)


def pe_model_value(*, eps: float, industry_pe: float, relative_pe_score: float = 1.0) -> IntrinsicValueRange:
    adjusted_pe = _clip(industry_pe * relative_pe_score, *PE_CLIP_RANGE)
    return _range_from_mid(max(0.0, eps * adjusted_pe), "PE")


def pb_model_value(
    *,
    book_value_per_share: float,
    roe: float,
    industry_pb: float,
    roe_baseline: float = DEFAULT_ROE_BASELINE,
) -> IntrinsicValueRange:
    roe_adjustment = roe / roe_baseline if roe_baseline > 0 else 1.0
    adjusted_pb = _clip(industry_pb * roe_adjustment, *PB_CLIP_RANGE)
    return _range_from_mid(max(0.0, book_value_per_share * adjusted_pb), "PB")


def _dcf_value(*, fcf_per_share: float, growth_rate: float, discount_rate: float) -> float:
    safe_growth = min(growth_rate, discount_rate - 0.01)
    if discount_rate <= safe_growth:
        return 0.0
    next_fcf = max(0.0, fcf_per_share) * (1.0 + safe_growth)
    return next_fcf / (discount_rate - safe_growth)


def simple_dcf_value(
    *,
    fcf_per_share: float,
    growth_rate: float,
    terminal_growth: float = DCF_TERMINAL_GROWTH,
) -> IntrinsicValueRange:
    conservative_growth = min(growth_rate, terminal_growth)
    optimistic_growth = max(terminal_growth, min(growth_rate, DCF_DISCOUNT_RATE_LOW - 0.01))
    low = _dcf_value(
        fcf_per_share=fcf_per_share,
        growth_rate=conservative_growth,
        discount_rate=DCF_DISCOUNT_RATE_HIGH,
    )
    mid = _dcf_value(
        fcf_per_share=fcf_per_share,
        growth_rate=terminal_growth,
        discount_rate=DCF_DISCOUNT_RATE_MID,
    )
    high = _dcf_value(
        fcf_per_share=fcf_per_share,
        growth_rate=optimistic_growth,
        discount_rate=DCF_DISCOUNT_RATE_LOW,
    )
    values = sorted([low, mid, high])
    return IntrinsicValueRange(low=values[0], mid=values[1], high=values[2], method="DCF")


def combine_value_ranges(
    ranges: list[IntrinsicValueRange],
    weights: list[float] | None = None,
) -> IntrinsicValueRange:
    if not ranges:
        return IntrinsicValueRange(0.0, 0.0, 0.0, "combined")
    if weights is None:
        weights = [1.0] * len(ranges)
    if len(weights) != len(ranges):
        raise ValueError("weights length must match ranges length")
    total_weight = sum(max(0.0, weight) for weight in weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive value")

    def weighted(values: list[float]) -> float:
        return sum(value * max(0.0, weight) for value, weight in zip(values, weights, strict=True)) / total_weight

    return IntrinsicValueRange(
        low=weighted([item.low for item in ranges]),
        mid=weighted([item.mid for item in ranges]),
        high=weighted([item.high for item in ranges]),
        method="+".join(item.method for item in ranges),
    )
