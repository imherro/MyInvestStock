from __future__ import annotations

from dataclasses import dataclass

from core.valuation import ValuationSignal


@dataclass(frozen=True)
class ConclusionRuleResult:
    grade: str
    confidence: float
    summary: str


def _score(value: float) -> float:
    return max(0.0, min(100.0, value))


def _confidence(value: float) -> float:
    return round(_score(value) / 100.0, 4)


def build_conclusion(signal: ValuationSignal) -> ConclusionRuleResult:
    undervalued = _score(signal.undervalued_score)
    growth = _score(signal.growth_score)
    quality = _score(signal.quality_score)
    risk_adjusted = _score(signal.risk_adjusted_score)

    if undervalued >= 70.0 and quality >= 60.0 and risk_adjusted >= 65.0:
        grade = "核心仓研究资格"
    elif quality >= 50.0 and risk_adjusted >= 55.0:
        grade = "可跟踪"
    elif undervalued <= 25.0 and risk_adjusted < 45.0:
        grade = "高估暂缓"
    elif risk_adjusted >= 40.0:
        grade = "观察"
    else:
        grade = "不具备"

    summary = (
        f"确定性规则评分：估值安全 {undervalued:.1f}，增长 {growth:.1f}，"
        f"质量 {quality:.1f}，风险调整 {risk_adjusted:.1f}。"
    )
    return ConclusionRuleResult(grade=grade, confidence=_confidence(risk_adjusted), summary=summary)
