from __future__ import annotations

import inspect
import unittest

from core.report import REPORT_VERSION, build_stock_report
from core.report import assembler as assembler_module
from core.schema.stock_report import StockResearchReport
from core.task.state import compute_task_run_id


ASSEMBLY_INPUT = {
    "stock_code": "600519.SH",
    "stock_name": "贵州茅台",
    "source_report_id": "leader_review_2026-06-24",
    "task_type": "financial",
    "research_date": "2026-06-24",
    "industry_position": "高端白酒龙头，品牌和渠道优势仍明显。",
    "competition_landscape": "主要竞争来自五粮液、泸州老窖及区域高端酒企。",
    "upstream_downstream": "上游粮食和包材影响较小，下游渠道议价和库存是关键。",
    "financial_rows": [
        {
            "revenue": 100.0,
            "net_profit": 10.0,
            "equity": 50.0,
            "gross_margin": 0.45,
            "debt": 20.0,
            "free_cash_flow": 8.0,
            "market_cap": 200.0,
        },
        {
            "revenue": 120.0,
            "net_profit": 13.0,
            "equity": 60.0,
            "gross_margin": 0.47,
            "debt": 22.0,
            "free_cash_flow": 10.0,
            "market_cap": 220.0,
        },
        {
            "revenue": 145.0,
            "net_profit": 17.0,
            "equity": 70.0,
            "gross_margin": 0.48,
            "debt": 25.0,
            "free_cash_flow": 13.0,
            "market_cap": 260.0,
        },
        {
            "revenue": 175.0,
            "net_profit": 22.0,
            "equity": 84.0,
            "gross_margin": 0.49,
            "debt": 28.0,
            "free_cash_flow": 16.0,
            "market_cap": 300.0,
        },
    ],
    "valuation_inputs": {
        "current_price": 90.0,
        "stock_pe": 20.0,
        "pe": 20.0,
        "pb": 2.0,
        "eps": 5.0,
        "book_value_per_share": 30.0,
        "industry_pb": 2.0,
        "fcf_per_share": 4.0,
    },
    "peers": [
        {"stock_code": "000858.SZ", "pe": 18.0, "roe": 0.16},
        {"stock_code": "000568.SZ", "pe": 22.0, "roe": 0.20},
        {"stock_code": "600809.SH", "pe": 28.0, "roe": 0.14},
    ],
    "risk_signals": {
        "financial_risk": "增长放缓压缩估值弹性。",
        "industry_risk": "白酒需求周期和渠道库存波动。",
        "sentiment_risk": "消费风格回落时估值承压。",
        "invalidation_conditions": ["收入连续低于行业", "批价持续下行"],
    },
}


class ReportAssemblyTests(unittest.TestCase):
    def test_build_stock_report_is_schema_first_and_deterministic(self) -> None:
        first = build_stock_report(ASSEMBLY_INPUT)
        second = build_stock_report(ASSEMBLY_INPUT)

        self.assertIsInstance(first, StockResearchReport)
        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))
        self.assertEqual(first.report_version, REPORT_VERSION)
        self.assertEqual(len(first.report_hash or ""), 64)
        self.assertEqual(
            first.run_id,
            compute_task_run_id("600519.SH", "financial", "2026-06-24", "stock_research_report.v1"),
        )
        self.assertEqual(first.heavy_position_view, first.conclusion.grade)
        self.assertGreater(first.valuation.intrinsic_value_mid or 0.0, 0.0)
        self.assertIsNotNone(first.valuation.risk_adjusted_score)

    def test_report_hash_changes_when_feature_inputs_change(self) -> None:
        baseline = build_stock_report(ASSEMBLY_INPUT)
        changed_input = {
            **ASSEMBLY_INPUT,
            "financial_rows": [
                *ASSEMBLY_INPUT["financial_rows"][:-1],
                {**ASSEMBLY_INPUT["financial_rows"][-1], "net_profit": 25.0},
            ],
        }
        changed = build_stock_report(changed_input)

        self.assertNotEqual(baseline.report_hash, changed.report_hash)

    def test_assembly_module_has_no_model_or_prompt_runtime_dependency(self) -> None:
        source = inspect.getsource(assembler_module).lower()

        self.assertNotIn("openai", source)
        self.assertNotIn("chat.completions", source)
        self.assertNotIn("responses.create", source)
        self.assertNotIn("prompt_template", source)


if __name__ == "__main__":
    unittest.main()
