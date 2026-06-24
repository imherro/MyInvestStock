from __future__ import annotations

from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myinveststock.db import connect, init_db, list_daily_prices, upsert_daily_prices
from myinveststock.leader_index import (
    build_financial_prompt,
    build_report_explainer_prompt,
    build_strategic_prompt,
    primary_items,
    report_meta,
)
from myinveststock.web import (
    FOOTER_SCRIPT_URL,
    STATIC_ASSET_VERSION,
    leader_to_summary,
    render_layout,
    render_queue_rows,
    render_valuation_chart,
    research_run_to_summary,
    xueqiu_stock_link,
    xueqiu_url_for_code,
)


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
        self.assertIn("task_type 固定为 financial", prompt)
        self.assertIn("assembly_input", prompt)
        self.assertIn("不要手写最终 StockResearchReport", prompt)
        self.assertIn("scripts/build_research_report.py --audit-db", prompt)
        self.assertIn("不能重新计算估值", prompt)

    def test_report_explainer_prompt_is_interpreter_only(self) -> None:
        prompt = build_report_explainer_prompt({"stock_code": "600519.SH", "report_hash": "abc"})
        self.assertIn("A 股研究报告解释器", prompt)
        self.assertIn("不得修改任何数值", prompt)
        self.assertIn("不得重新估值", prompt)
        self.assertIn("不得引入新外部数据", prompt)
        self.assertIn('"stock_code": "600519.SH"', prompt)

    def test_report_id_required(self) -> None:
        with self.assertRaises(ValueError):
            report_meta({"report": {}})

    def test_footer_script_is_in_layout(self) -> None:
        page = render_layout("title", "<p>body</p>").decode("utf-8")
        self.assertIn(f'<script src="{FOOTER_SCRIPT_URL}" defer></script>', page)
        self.assertIn(f'/static/styles.css?v={STATIC_ASSET_VERSION}', page)

    def test_queue_rows_link_to_stock_page(self) -> None:
        html = render_queue_rows(
            [
                {
                    "priority": 1,
                    "stage": 2,
                    "code": "600519.SH",
                    "name": "贵州茅台",
                    "task_type": "financial",
                    "status": "pending",
                    "task_keyword": "MyInvestStock 个股财务估值深研 600519.SH 贵州茅台",
                }
            ]
        )
        self.assertIn('href="/stocks/600519.SH"', html)
        self.assertIn('href="https://xueqiu.com/S/SH600519"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn(">贵州茅台</a>", html)

    def test_stock_code_links_to_xueqiu_new_window(self) -> None:
        self.assertEqual(xueqiu_url_for_code("603259.SH"), "https://xueqiu.com/S/SH603259")
        self.assertEqual(xueqiu_url_for_code("300750.SZ"), "https://xueqiu.com/S/SZ300750")
        link = xueqiu_stock_link("688256.SH")
        self.assertIn('href="https://xueqiu.com/S/SH688256"', link)
        self.assertIn('target="_blank"', link)
        self.assertIn('rel="noopener noreferrer"', link)

    def test_valuation_chart_uses_time_price_svg(self) -> None:
        html = render_valuation_chart(
            [
                {
                    "research_date": "2026-06-22",
                    "valuation_low": 90,
                    "valuation_mid": 120,
                    "valuation_high": 150,
                    "valuation_method": "PE",
                    "heavy_position_view": "可跟踪",
                },
                {
                    "research_date": "2026-06-24",
                    "valuation_low": 100,
                    "valuation_mid": 130,
                    "valuation_high": 160,
                    "valuation_method": "PE+DCF",
                    "heavy_position_view": "核心仓研究资格",
                },
            ]
        )
        self.assertIn("<svg", html)
        self.assertIn("合理估值区间随时间变化图", html)
        self.assertIn("valuation-band", html)
        self.assertIn("valuation-mid-line", html)
        self.assertIn("价格 CNY/share", html)
        self.assertIn("06-22", html)
        self.assertIn("06-24", html)

    def test_valuation_chart_uses_kline_overlay_when_prices_exist(self) -> None:
        runs = [
            {
                "research_date": "2026-06-22",
                "valuation_low": 90,
                "valuation_mid": 120,
                "valuation_high": 150,
                "valuation_method": "PE",
                "heavy_position_view": "可跟踪",
            },
            {
                "research_date": "2026-06-24",
                "valuation_low": 100,
                "valuation_mid": 130,
                "valuation_high": 160,
                "valuation_method": "PE+DCF",
                "heavy_position_view": "核心仓研究资格",
            },
        ]
        prices = [
            {
                "trade_date": "2026-06-21",
                "open_price": 105,
                "high_price": 108,
                "low_price": 101,
                "close_price": 107,
            },
            {
                "trade_date": "2026-06-22",
                "open_price": 107,
                "high_price": 111,
                "low_price": 106,
                "close_price": 109,
            },
            {
                "trade_date": "2026-06-24",
                "open_price": 110,
                "high_price": 116,
                "low_price": 109,
                "close_price": 112,
            },
        ]
        html = render_valuation_chart(runs, prices)
        self.assertIn("K线叠加合理估值区间图", html)
        self.assertIn("kline-layer", html)
        self.assertIn("valuation-step-band", html)
        self.assertIn("legend-kline", html)
        self.assertIn("财务深研刷新点", html)

    def test_single_valuation_chart_uses_whisker_without_band(self) -> None:
        html = render_valuation_chart(
            [
                {
                    "research_date": "2026-06-24",
                    "valuation_low": 100,
                    "valuation_mid": 130,
                    "valuation_high": 160,
                    "valuation_method": "PE+DCF",
                    "heavy_position_view": "可跟踪",
                }
            ]
        )
        self.assertIn("valuation-whisker", html)
        self.assertIn("valuation-mid-dot", html)
        self.assertNotIn("valuation-band", html)

    def test_daily_price_cache_roundtrip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "prices.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                count = upsert_daily_prices(
                    conn,
                    code="600519.SH",
                    rows=[
                        {
                            "trade_date": "20260621",
                            "open": 1500,
                            "high": 1520,
                            "low": 1490,
                            "close": 1510,
                            "vol": 100,
                            "amount": 200,
                        },
                        {
                            "trade_date": "2026-06-22",
                            "open": 1510,
                            "high": 1530,
                            "low": 1500,
                            "close": 1525,
                        },
                    ],
                    source="unit-test",
                    adj="qfq",
                )
                conn.commit()
                rows = list_daily_prices(conn, "600519.SH", limit=5)
            self.assertEqual(count, 2)
            self.assertEqual([row["trade_date"] for row in rows], ["2026-06-21", "2026-06-22"])
            self.assertEqual(rows[0]["adj"], "qfq")

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
