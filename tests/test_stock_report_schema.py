from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from core.schema.stock_report import StockResearchReport, validate_stock_research_report
from myinveststock.db import connect, init_db, insert_research_run


def valid_financial_sample() -> dict[str, object]:
    return {
        "schema_version": "stock_research_report.v1",
        "run_id": "run-600519-20260624",
        "stock_code": "600519.SH",
        "stock_name": "贵州茅台",
        "source_report_id": "leader_review_2026-06-24",
        "task_type": "financial",
        "research_date": "2026-06-24",
        "status": "complete",
        "title": "贵州茅台财务估值深研",
        "summary": "现金流质量较高，估值需要结合增长放缓重新定价。",
        "industry_position": "高端白酒龙头，品牌和渠道优势仍明显。",
        "competition_landscape": "主要竞争来自五粮液、泸州老窖及区域高端酒企。",
        "upstream_downstream": "上游粮食和包材影响较小，下游渠道议价和库存是关键。",
        "annual_growth": "收入和利润增速进入中低速阶段，质量高于速度。",
        "multi_bagger_potential": "五倍潜力取决于利润复合增长和估值中枢修复。",
        "heavy_position_view": "可跟踪",
        "fundamentals": {
            "revenue_growth": 0.09,
            "profit_growth": 0.10,
            "roe": 0.30,
            "debt_ratio": 0.18,
            "revenue_quality": "收入确认稳定，渠道库存需要跟踪。",
            "profit_quality": "利润率高，费用率可控。",
            "cash_flow_quality": "经营现金流质量较好。",
            "balance_sheet_quality": "负债率低，账面现金充足。",
        },
        "valuation": {
            "pe": 22.0,
            "pb": 8.0,
            "peg": 2.2,
            "intrinsic_value_low": 1200.0,
            "intrinsic_value_mid": 1500.0,
            "intrinsic_value_high": 1800.0,
            "unit": "CNY/share",
            "method": "PE",
            "confidence": "medium",
            "key_assumptions": ["利润保持中低速增长", "估值中枢不继续系统性下移"],
        },
        "peer_comparison": {
            "industry_rank": 1,
            "competitors": ["五粮液", "泸州老窖"],
            "relative_valuation": "估值溢价来自品牌确定性。",
            "competitive_position": "品牌、渠道和现金流领先。",
        },
        "risk": {
            "financial_risk": "增长放缓压缩估值弹性。",
            "industry_risk": "白酒需求周期和渠道库存波动。",
            "sentiment_risk": "消费风格回落时估值承压。",
            "invalidation_conditions": ["收入连续低于行业", "批价持续下行"],
        },
        "conclusion": {
            "grade": "可跟踪",
            "confidence": 0.78,
            "summary": "适合持续跟踪，但需要等待估值和增长重新匹配。",
        },
        "evidence": [
            {
                "source": "Tushare",
                "date": "2026-06-24",
                "url": "local .env authenticated Tushare query",
                "purpose": "确认财务与估值基础数据",
                "detail": "使用结构化财务、行情和估值数据作为主源。",
            }
        ],
        "assumptions": ["不输出交易指令", "估值区间只作为研究结论"],
    }


class StockReportSchemaTests(unittest.TestCase):
    def test_valid_sample_passes_schema(self) -> None:
        report = validate_stock_research_report(valid_financial_sample())
        self.assertIsInstance(report, StockResearchReport)
        self.assertEqual(report.stock_code, "600519.SH")
        self.assertEqual(report.model_dump(mode="json")["valuation"]["intrinsic_value_mid"], 1500.0)

    def test_invalid_sample_rejects_extra_dict_passthrough(self) -> None:
        sample = valid_financial_sample()
        sample["unexpected_payload"] = {"free": "dict"}
        with self.assertRaises(ValidationError):
            validate_stock_research_report(sample)

    def test_db_insert_rejects_raw_dict_and_accepts_validated_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                with self.assertRaises(TypeError):
                    insert_research_run(conn, valid_financial_sample())  # type: ignore[arg-type]

                report = validate_stock_research_report(valid_financial_sample())
                row_id = insert_research_run(conn, report)
                row = conn.execute(
                    "SELECT code, task_type, valuation_mid, raw_json FROM stock_research_runs WHERE id = ?",
                    (row_id,),
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["code"], "600519.SH")
        self.assertEqual(row["task_type"], "financial")
        self.assertEqual(row["valuation_mid"], 1500.0)
        self.assertIn("stock_research_report.v1", row["raw_json"])

    def test_insert_requires_validated_schema_even_for_sqlite_connection(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(TypeError):
                insert_research_run(conn, {"stock_code": "600519.SH"})  # type: ignore[arg-type]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
