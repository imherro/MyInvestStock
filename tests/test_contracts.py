from __future__ import annotations

import unittest

from myinveststock.leader_index import build_financial_prompt, build_strategic_prompt, primary_items, report_meta
from myinveststock.web import FOOTER_SCRIPT_URL, render_layout


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


if __name__ == "__main__":
    unittest.main()
