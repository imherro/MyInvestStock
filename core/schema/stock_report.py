from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, model_validator

from core.task.state import compute_task_run_id


STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TaskType = Literal["strategic", "financial"]
RunStatus = Literal["complete", "draft", "blocked"]
Confidence = Literal["low", "medium", "high"]
HeavyPositionView = Literal["不具备", "观察", "可跟踪", "核心仓研究资格", "高估暂缓"]


class StrictSchemaModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EvidenceItem(StrictSchemaModel):
    source: StrictStr
    date: StrictStr | None = None
    url: StrictStr | None = None
    purpose: StrictStr
    detail: StrictStr


class Fundamentals(StrictSchemaModel):
    revenue_growth: StrictFloat | None = None
    profit_growth: StrictFloat | None = None
    roe: StrictFloat | None = None
    debt_ratio: StrictFloat | None = None
    revenue_quality: StrictStr
    profit_quality: StrictStr
    cash_flow_quality: StrictStr
    balance_sheet_quality: StrictStr


class Valuation(StrictSchemaModel):
    pe: StrictFloat | None = None
    pb: StrictFloat | None = None
    peg: StrictFloat | None = None
    intrinsic_value_low: StrictFloat | None = None
    intrinsic_value_mid: StrictFloat | None = None
    intrinsic_value_high: StrictFloat | None = None
    unit: StrictStr = "CNY/share"
    method: StrictStr
    confidence: Confidence
    key_assumptions: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_range_order(self) -> Valuation:
        values = [self.intrinsic_value_low, self.intrinsic_value_mid, self.intrinsic_value_high]
        if all(value is not None for value in values):
            low, mid, high = values
            if not (low <= mid <= high):
                raise ValueError("valuation range must satisfy low <= mid <= high")
        elif any(value is not None for value in values):
            raise ValueError("valuation range must provide low, mid, and high together")
        return self


class PeerComparison(StrictSchemaModel):
    industry_rank: StrictInt | None = Field(default=None, ge=1)
    competitors: list[StrictStr] = Field(default_factory=list)
    relative_valuation: StrictStr
    competitive_position: StrictStr


class Risk(StrictSchemaModel):
    financial_risk: StrictStr
    industry_risk: StrictStr
    sentiment_risk: StrictStr
    invalidation_conditions: list[StrictStr] = Field(default_factory=list)


class Conclusion(StrictSchemaModel):
    grade: HeavyPositionView
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    summary: StrictStr


class StockResearchReport(StrictSchemaModel):
    schema_version: Literal["stock_research_report.v1"] = "stock_research_report.v1"
    run_id: StrictStr | None = None
    stock_code: StrictStr
    stock_name: StrictStr
    source_report_id: StrictStr | None = None
    task_type: TaskType
    research_date: StrictStr
    status: RunStatus = "complete"
    title: StrictStr
    summary: StrictStr
    industry_position: StrictStr
    competition_landscape: StrictStr
    upstream_downstream: StrictStr
    annual_growth: StrictStr
    multi_bagger_potential: StrictStr
    heavy_position_view: HeavyPositionView
    fundamentals: Fundamentals
    valuation: Valuation
    peer_comparison: PeerComparison
    risk: Risk
    conclusion: Conclusion
    evidence: list[EvidenceItem] = Field(min_length=1)
    assumptions: list[StrictStr] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_report(self) -> StockResearchReport:
        if not STOCK_CODE_RE.match(self.stock_code):
            raise ValueError("stock_code must match 000000.SH/SZ/BJ")
        if not DATE_RE.match(self.research_date):
            raise ValueError("research_date must use YYYY-MM-DD")
        if self.heavy_position_view != self.conclusion.grade:
            raise ValueError("heavy_position_view must equal conclusion.grade")

        valuation_fields = (
            self.valuation.intrinsic_value_low,
            self.valuation.intrinsic_value_mid,
            self.valuation.intrinsic_value_high,
        )
        has_range = all(value is not None for value in valuation_fields)
        if self.task_type == "strategic" and has_range:
            raise ValueError("strategic research must not write valuation range")
        if self.task_type == "financial" and not has_range:
            raise ValueError("financial research must include a complete valuation range")

        expected_run_id = compute_task_run_id(self.stock_code, self.task_type, self.research_date, self.schema_version)
        if self.run_id is None:
            object.__setattr__(self, "run_id", expected_run_id)
        elif self.run_id != expected_run_id:
            raise ValueError("run_id must equal hash(stock_code + task_type + date + schema_version)")
        return self


def validate_stock_research_report(raw_output: dict[str, Any]) -> StockResearchReport:
    return StockResearchReport(**raw_output)
