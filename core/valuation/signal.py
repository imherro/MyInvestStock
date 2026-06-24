from __future__ import annotations

from dataclasses import dataclass

from .features import FundamentalFeatures
from .peer import PeerComparisonResult


QUALITY_ROE_TARGET = 0.20
GROWTH_TARGET = 0.20
DEBT_TO_EQUITY_LIMIT = 1.0


@dataclass(frozen=True)
class ValuationSignal:
    undervalued_score: float
    growth_score: float
    quality_score: float
    risk_adjusted_score: float


def _score(value: float) -> float:
    return max(0.0, min(100.0, value))


def build_valuation_signal(
    *,
    current_price: float,
    intrinsic_mid: float,
    features: FundamentalFeatures,
    peer_comparison: PeerComparisonResult,
) -> ValuationSignal:
    if current_price <= 0 or intrinsic_mid <= 0:
        undervalued_score = 0.0
    else:
        undervalued_score = _score((intrinsic_mid / current_price - 1.0) * 100.0 + 50.0)

    blended_growth = (features.revenue_growth_3y + features.profit_growth_3y) / 2.0
    growth_score = _score((blended_growth / GROWTH_TARGET) * 100.0)

    roe_score = _score((features.roe_avg / QUALITY_ROE_TARGET) * 70.0)
    margin_score = _score(features.gross_margin * 30.0)
    quality_score = _score(roe_score + margin_score)

    leverage_penalty = _score((features.debt_to_equity / DEBT_TO_EQUITY_LIMIT) * 20.0)
    peer_bonus = peer_comparison.ranking_percentile * 10.0
    risk_adjusted_score = _score(
        undervalued_score * 0.35
        + growth_score * 0.25
        + quality_score * 0.30
        + peer_bonus
        - leverage_penalty
    )

    return ValuationSignal(
        undervalued_score=undervalued_score,
        growth_score=growth_score,
        quality_score=quality_score,
        risk_adjusted_score=risk_adjusted_score,
    )
