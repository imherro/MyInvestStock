from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class PeerMetrics:
    stock_code: str
    pe: float
    roe: float


@dataclass(frozen=True)
class PeerComparisonResult:
    industry_median_pe: float
    industry_median_roe: float
    pe_percentile: float
    roe_percentile: float
    ranking_percentile: float


def _percentile(value: float, values: list[float], *, higher_is_better: bool) -> float:
    if not values:
        return 0.0
    if higher_is_better:
        better_or_equal = sum(1 for item in values if value >= item)
    else:
        better_or_equal = sum(1 for item in values if value <= item)
    return better_or_equal / len(values)


def compare_to_peers(*, stock_pe: float, stock_roe: float, peers: list[PeerMetrics]) -> PeerComparisonResult:
    peer_pes = [peer.pe for peer in peers if peer.pe > 0]
    peer_roes = [peer.roe for peer in peers]
    industry_median_pe = float(median(peer_pes)) if peer_pes else 0.0
    industry_median_roe = float(median(peer_roes)) if peer_roes else 0.0
    pe_percentile = _percentile(stock_pe, peer_pes, higher_is_better=False)
    roe_percentile = _percentile(stock_roe, peer_roes, higher_is_better=True)
    ranking_percentile = (pe_percentile + roe_percentile) / 2.0
    return PeerComparisonResult(
        industry_median_pe=industry_median_pe,
        industry_median_roe=industry_median_roe,
        pe_percentile=pe_percentile,
        roe_percentile=roe_percentile,
        ranking_percentile=ranking_percentile,
    )
