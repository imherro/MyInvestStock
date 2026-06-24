from __future__ import annotations

import unittest

from myinveststock.leader_index import build_financial_prompt, build_strategic_prompt, primary_items, report_meta
from myinveststock.web import FOOTER_SCRIPT_URL, STATIC_ASSET_VERSION, leader_to_summary, render_layout, research_run_to_summary


class ContractTests(unittest.TestCase):
    def test_primary_items_uses_api_index_path(self) -> None:
        payload = {
            "report": {"report_id": "r1"},
            "key_results": {
                "primary_output": {
                    "items": [
                        {"code": "603259.SH", "name": "药明康德", "deep_score": 75.34},
                        {"code": "bad", "name": "bad"},
                    ]
                }
            },
        }
        items = primary_items(payload)
        self.assertEqual([item["code"] for item in items], ["603259.SH"])

    def test_strategic_prompt_is_one_stock_only(self) -> None:
        report = {"report_id": "r1", "basis_date": "2026-06-22"}
        item = {"code": "600519.SH", "name": "贵州茅台", "theme": "消费/传媒"}
        prompt = build_strategic_prompt(item, report)
        self.assertIn("唯一研究对象：600519.SH 贵州茅台", prompt)
        self.assertIn("禁止同时研究其他 A可跟踪龙头", prompt)
        self.assertIn("key_results.primary_output.items", prompt)
        self.assertIn("不给最终估值区间", prompt)

    def test_financial_prompt_depends_on_strategic(self) -> None:
        report = {"report_id": "r1", "basis_date": "2026-06-22"}
        item = {"code": "600519.SH", "name": "贵州茅台", "theme": "消费/传媒"}
        prompt = build_financial_prompt(item, report)
        self.assertIn("task_type='strategic'", prompt)
        self.assertIn("task_type='financial'", prompt)
        self.assertIn("合理估值区间", prompt)

    def test_report_id_required(self) -> None:
        with self.assertRaises(ValueError):
            report_meta({"report": {}})

    def test_footer_script_is_in_layout(self) -> None:
        page = render_layout("title", "<p>body</p>").decode("utf-8")
        self.assertIn(f'<script src="{FOOTER_SCRIPT_URL}" defer></script>', page)
        self.assertIn(f'/static/styles.css?v={STATIC_ASSET_VERSION}', page)

    def test_index_leader_summary_contract(self) -> None:
        row = {
            "code": "603259.SH",
            "name": "药明康德",
            "theme": "创新药/医药",
            "themes_json": '["创新药/医药"]',
            "deep_rating": "A",
            "deep_label": "可跟踪龙头",
            "deep_score": 75.34,
            "shadow_observation_eligible": 1,
            "candidate_leader_tier": "证据确认龙头",
            "candidate_leader_claim": "CXO龙头",
            "candidate_evidence_score": 86.55,
            "candidate_evidence_count": 4,
            "candidate_hard_evidence_count": 3,
            "market_json": '{"close":106.83}',
            "scores_json": '{"valuation_safety":83.28}',
            "risk_flags_json": "[]",
            "data_gaps_json": "[]",
            "xueqiu_url": "https://xueqiu.com/S/SH603259",
        }
        summary = leader_to_summary(row)
        self.assertEqual(summary["code"], "603259.SH")
        self.assertEqual(summary["links"]["api"], "/api/stocks/603259.SH")
        self.assertEqual(summary["market"]["close"], 106.83)

    def test_latest_research_summary_contract(self) -> None:
        row = {
            "id": 1,
            "task_type": "financial",
            "research_date": "2026-06-24",
            "status": "complete",
            "title": "财务估值深研",
            "summary": "示例",
            "valuation_low": 90,
            "valuation_mid": 120,
            "valuation_high": 150,
            "valuation_unit": "CNY/share",
            "valuation_method": "PE",
            "valuation_confidence": "medium",
            "industry_position": None,
            "competition_landscape": None,
            "upstream_downstream": None,
            "annual_growth": "收入增长",
            "multi_bagger_potential": "需要盈利扩张",
            "heavy_position_view": "可跟踪",
            "evidence_json": "[]",
            "assumptions_json": "[]",
            "risks_json": '["估值收缩"]',
        }
        summary = research_run_to_summary(row)
        self.assertEqual(summary["task_type"], "financial")
        self.assertEqual(summary["valuation"]["mid"], 120)
        self.assertEqual(summary["risks"], ["估值收缩"])


if __name__ == "__main__":
    unittest.main()
