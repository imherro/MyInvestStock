from __future__ import annotations

import unittest

from core.schema.stock_report import validate_stock_research_report
from core.task.state import compute_task_run_id
from core.valuation import (
    PeerMetrics,
    build_valuation_signal,
    combine_value_ranges,
    compare_to_peers,
    extract_fundamental_features,
    pb_model_value,
    pe_model_value,
    simple_dcf_value,
)


FINANCIAL_ROWS = [
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
]


class ValuationEngineTests(unittest.TestCase):
    def test_feature_engine_is_deterministic(self) -> None:
        first = extract_fundamental_features(FINANCIAL_ROWS)
        second = extract_fundamental_features(FINANCIAL_ROWS)
        self.assertEqual(first, second)
        self.assertGreater(first.revenue_growth_3y, 0.0)
        self.assertGreater(first.profit_growth_3y, first.revenue_growth_3y)
        self.assertGreater(first.roe_avg, 0.0)

    def test_valuation_models_return_ordered_ranges(self) -> None:
        pe_range = pe_model_value(eps=5.0, industry_pe=20.0, relative_pe_score=1.1)
        pb_range = pb_model_value(book_value_per_share=30.0, roe=0.18, industry_pb=2.0)
        dcf_range = simple_dcf_value(fcf_per_share=4.0, growth_rate=0.05)
        combined = combine_value_ranges([pe_range, pb_range, dcf_range], weights=[0.4, 0.3, 0.3])

        for value_range in [pe_range, pb_range, dcf_range, combined]:
            self.assertLessEqual(value_range.low, value_range.mid)
            self.assertLessEqual(value_range.mid, value_range.high)
            self.assertGreater(value_range.high, 0.0)

    def test_peer_comparison_and_signal_are_deterministic(self) -> None:
        features = extract_fundamental_features(FINANCIAL_ROWS)
        peers = [
            PeerMetrics("A", pe=18.0, roe=0.16),
            PeerMetrics("B", pe=22.0, roe=0.20),
            PeerMetrics("C", pe=28.0, roe=0.14),
        ]
        peer_result = compare_to_peers(stock_pe=20.0, stock_roe=features.roe_avg, peers=peers)
        signal_a = build_valuation_signal(
            current_price=90.0,
            intrinsic_mid=120.0,
            features=features,
            peer_comparison=peer_result,
        )
        signal_b = build_valuation_signal(
            current_price=90.0,
            intrinsic_mid=120.0,
            features=features,
            peer_comparison=peer_result,
        )

        self.assertEqual(signal_a, signal_b)
        self.assertGreaterEqual(signal_a.risk_adjusted_score, 0.0)
        self.assertLessEqual(signal_a.risk_adjusted_score, 100.0)

    def test_valuation_signal_can_be_consumed_by_stock_report_schema(self) -> None:
        features = extract_fundamental_features(FINANCIAL_ROWS)
        peer_result = compare_to_peers(
            stock_pe=20.0,
            stock_roe=features.roe_avg,
            peers=[PeerMetrics("A", pe=18.0, roe=0.16), PeerMetrics("B", pe=22.0, roe=0.20)],
        )
        value_range = combine_value_ranges(
            [
                pe_model_value(eps=5.0, industry_pe=20.0, relative_pe_score=1.0),
                pb_model_value(book_value_per_share=30.0, roe=features.roe_avg, industry_pb=2.0),
                simple_dcf_value(fcf_per_share=4.0, growth_rate=features.profit_growth_3y),
            ]
        )
        signal = build_valuation_signal(
            current_price=90.0,
            intrinsic_mid=value_range.mid,
            features=features,
            peer_comparison=peer_result,
        )
        report = validate_stock_research_report(
            {
                "schema_version": "stock_research_report.v1",
                "stock_code": "600519.SH",
                "stock_name": "贵州茅台",
                "source_report_id": "leader_review_2026-06-24",
                "task_type": "stock_research",
                "research_date": "2026-06-24",
                "trigger_reason": "新进入可跟踪龙头",
                "status": "complete",
                "title": "贵州茅台 deterministic valuation report",
                "summary": "估值区间和 signal 由确定性估值引擎生成。",
                "industry_position": "高端白酒龙头。",
                "competition_landscape": "与高端白酒同业对比。",
                "upstream_downstream": "渠道库存和终端需求是核心变量。",
                "annual_growth": "增长率来自特征提取层。",
                "multi_bagger_potential": "潜力由增长和估值共同约束。",
                "heavy_position_view": "可跟踪",
                "fundamentals": {
                    "revenue_growth": features.revenue_growth_3y,
                    "profit_growth": features.profit_growth_3y,
                    "roe": features.roe_avg,
                    "debt_ratio": features.debt_to_equity,
                    "revenue_quality": "deterministic feature",
                    "profit_quality": "deterministic feature",
                    "cash_flow_quality": "deterministic feature",
                    "balance_sheet_quality": "deterministic feature",
                },
                "valuation": {
                    "pe": 20.0,
                    "pb": 2.0,
                    "peg": 1.0,
                    "intrinsic_value_low": value_range.low,
                    "intrinsic_value_mid": value_range.mid,
                    "intrinsic_value_high": value_range.high,
                    "unit": "CNY/share",
                    "method": value_range.method,
                    "confidence": "medium",
                    "key_assumptions": ["deterministic valuation engine"],
                    "engine_version": "valuation_engine.v1",
                    "undervalued_score": signal.undervalued_score,
                    "growth_score": signal.growth_score,
                    "quality_score": signal.quality_score,
                    "risk_adjusted_score": signal.risk_adjusted_score,
                },
                "peer_comparison": {
                    "industry_rank": 1,
                    "competitors": ["A", "B"],
                    "relative_valuation": f"median PE {peer_result.industry_median_pe:.2f}",
                    "competitive_position": f"percentile {peer_result.ranking_percentile:.2f}",
                },
                "risk": {
                    "financial_risk": "feature risk",
                    "industry_risk": "peer risk",
                    "sentiment_risk": "market risk",
                    "invalidation_conditions": ["deterministic input invalid"],
                },
                "conclusion": {
                    "grade": "可跟踪",
                    "confidence": 0.76,
                    "summary": "schema can consume deterministic valuation output.",
                },
                "evidence": [
                    {
                        "source": "unit-test",
                        "date": "2026-06-24",
                        "url": "local",
                        "purpose": "schema integration",
                        "detail": "valuation block generated by deterministic engine",
                    }
                ],
                "assumptions": ["same input gives same output"],
            }
        )
        self.assertEqual(
            report.run_id,
            compute_task_run_id("600519.SH", "stock_research", "2026-06-24", "stock_research_report.v1"),
        )
        self.assertEqual(report.valuation.engine_version, "valuation_engine.v1")
        self.assertEqual(report.valuation.risk_adjusted_score, signal.risk_adjusted_score)


if __name__ == "__main__":
    unittest.main()
