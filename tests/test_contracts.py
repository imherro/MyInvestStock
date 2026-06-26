from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from myinveststock.db import (
    QUEUE_SOURCE_REQUEST,
    QUEUE_SOURCE_TRACKABLE,
    TASK_TYPE_STOCK_RESEARCH,
    TRIGGER_MANUAL_REQUEST,
    TRIGGER_TRACKABLE_LEADER,
    connect,
    init_db,
    list_daily_prices,
    list_price_refresh_subjects,
    list_queue,
    latest_report,
    upsert_daily_prices,
    upsert_queue_item,
    upsert_report,
    upsert_trackable_leader,
)
from myinveststock.leader_index import (
    build_requested_stock_research_prompt,
    build_report_explainer_prompt,
    build_stock_research_prompt,
    enqueue_requested_stock,
    primary_items,
    report_meta,
)
from myinveststock.theme_index import enrich_leader_item, market_context_summary, theme_context_for, theme_report_meta
from myinveststock.web import (
    FOOTER_SCRIPT_URL,
    HEADER_SCRIPT_URL,
    STATIC_ASSET_VERSION,
    api_catalog_payload,
    decision_matrix_summary,
    enqueue_extracted_stock_codes,
    extract_stock_codes,
    financial_signal_summary,
    leader_to_summary,
    metric,
    normalize_stock_query,
    openapi_spec,
    render_api_summary_section,
    render_bulk_research_entry_section,
    render_layout,
    render_queue_rows,
    render_report_links,
    render_signal_matrix,
    render_valuation_details,
    render_valuation_chart,
    research_run_to_summary,
    upstream_signal_summary,
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

    def test_stock_research_prompt_is_one_stock_only(self) -> None:
        report = {"report_id": "r1", "basis_date": "2026-06-22"}
        item = {"code": "600519.SH", "name": "贵州茅台", "theme": "消费/传媒"}
        prompt = build_stock_research_prompt(item, report, trigger_reason=TRIGGER_TRACKABLE_LEADER)
        self.assertIn("唯一研究对象：600519.SH 贵州茅台", prompt)
        self.assertIn("禁止同时研究其他 A可跟踪龙头", prompt)
        self.assertIn("key_results.primary_output.items", prompt)
        self.assertIn("完整个股深研", prompt)
        self.assertIn("task_type 固定为 stock_research", prompt)
        self.assertIn("assembly_input", prompt)
        self.assertIn("不要手写最终 StockResearchReport", prompt)
        self.assertIn("scripts/build_research_report.py --audit-db", prompt)
        self.assertIn("不能重新计算估值", prompt)
        self.assertIn("数倍潜力", prompt)

    def test_report_explainer_prompt_is_interpreter_only(self) -> None:
        prompt = build_report_explainer_prompt({"stock_code": "600519.SH", "report_hash": "abc"})
        self.assertIn("A 股研究报告解释器", prompt)
        self.assertIn("不得修改任何数值", prompt)
        self.assertIn("不得重新估值", prompt)
        self.assertIn("不得引入新外部数据", prompt)
        self.assertIn('"stock_code": "600519.SH"', prompt)

    def test_requested_stock_prompts_do_not_require_api_index_membership(self) -> None:
        report = {"report_id": "manual_research_request_2026-06-24", "basis_date": "2026-06-24"}
        item = {"code": "002594.SZ", "name": "比亚迪"}
        prompt = build_requested_stock_research_prompt(item, report, trigger_reason=TRIGGER_MANUAL_REQUEST)
        self.assertIn("用户主动请求", prompt)
        self.assertIn("不要求出现在 /api/index", prompt)
        self.assertIn("task_type", prompt)
        self.assertIn("stock_research", prompt)
        self.assertNotIn("key_results.primary_output.items", prompt)

    def test_report_id_required(self) -> None:
        with self.assertRaises(ValueError):
            report_meta({"report": {}})

    def test_unified_header_and_footer_scripts_are_in_layout(self) -> None:
        page = render_layout("title", "<p>body</p>").decode("utf-8")
        self.assertIn("<div data-myinvest-header></div>", page)
        self.assertIn("<div data-myinvest-footer></div>", page)
        self.assertIn(
            f'<script src="{HEADER_SCRIPT_URL}" data-target="[data-myinvest-header]" defer></script>',
            page,
        )
        self.assertIn(
            f'<script src="{FOOTER_SCRIPT_URL}" data-target="[data-myinvest-footer]" defer></script>',
            page,
        )
        self.assertNotIn('class="app-header"', page)
        self.assertIn(f'/static/styles.css?v={STATIC_ASSET_VERSION}', page)

    def test_api_catalog_lists_public_endpoints_and_safety(self) -> None:
        payload = api_catalog_payload("http://127.0.0.1:8016")
        self.assertEqual(payload["system_name"], "MyInvestStock")
        self.assertEqual(payload["base_url"], "http://127.0.0.1:8016")
        self.assertEqual(payload["docs"]["docs"], "/docs")  # type: ignore[index]
        self.assertEqual(payload["docs"]["redoc"], "/redoc")  # type: ignore[index]
        self.assertEqual(payload["docs"]["openapi_json"], "/openapi.json")  # type: ignore[index]
        self.assertGreaterEqual(payload["total_endpoints"], 10)
        group_names = [group["name"] for group in payload["groups"]]  # type: ignore[index]
        self.assertIn("文档入口", group_names)
        self.assertIn("当前数据", group_names)
        self.assertIn("历史数据", group_names)
        self.assertIn("分析结果", group_names)
        self.assertIn("系统状态", group_names)
        endpoints = [
            endpoint
            for group in payload["groups"]  # type: ignore[index]
            for endpoint in group["endpoints"]
        ]
        self.assertTrue(any(endpoint["path"] == "/api" and endpoint["read_only"] for endpoint in endpoints))
        self.assertTrue(any(endpoint["path"] == "/api/index" and endpoint["read_only"] for endpoint in endpoints))
        self.assertTrue(any(endpoint["path"] == "/research?stock={code}" and not endpoint["read_only"] for endpoint in endpoints))
        self.assertTrue(any(endpoint["path"] == "/research/bulk" and endpoint["method"] == "POST" and not endpoint["read_only"] for endpoint in endpoints))
        self.assertIn("/api 只描述接口", " ".join(payload["safety"]["notes"]))  # type: ignore[index]

    def test_openapi_and_home_api_summary_use_catalog(self) -> None:
        spec = openapi_spec("http://127.0.0.1:8016")
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/api", spec["paths"])  # type: ignore[operator]
        self.assertIn("/api/index", spec["paths"])  # type: ignore[operator]
        self.assertIn("/research", spec["paths"])  # type: ignore[operator]
        self.assertIn("post", spec["paths"]["/research/bulk"])  # type: ignore[index]

        html = render_api_summary_section(api_catalog_payload(""))
        self.assertIn("接口说明", html)
        self.assertIn("/api", html)
        self.assertIn("公开接口", html)
        self.assertIn("安全边界", html)

    def test_bulk_research_entry_extracts_stock_codes(self) -> None:
        text = "五粮液 000858.SZ，重复 SZ000858；芯片 SH688041，另一种 688041.SH，裸代码 300750 和 688256"
        self.assertEqual(extract_stock_codes(text), ["000858.SZ", "688041.SH", "300750.SZ", "688256.SH"])
        self.assertEqual(normalize_stock_query({"stock": ["SZ000858"]}), ("000858.SZ", None))
        html = render_bulk_research_entry_section()
        self.assertIn('action="/research/bulk"', html)
        self.assertIn('name="stocks"', html)

    def test_bulk_research_entry_enqueues_new_codes_and_skips_existing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bulk.sqlite"
            with patch("myinveststock.leader_index.lookup_tushare_stock_name", side_effect=lambda code: {"000858.SZ": "五粮液", "688041.SH": "海光信息"}.get(code)):
                first = enqueue_extracted_stock_codes("000858.SZ SH688041 000858", db_path=db_path)
                second = enqueue_extracted_stock_codes("SZ000858 688041.SH", db_path=db_path)
            with closing(connect(db_path)) as conn:
                rows = list_queue(conn)
            self.assertEqual(first["queued_count"], 2)
            self.assertEqual(second["existing_count"], 2)
            self.assertEqual({row["code"] for row in rows}, {"000858.SZ", "688041.SH"})
            self.assertEqual({row["source_type"] for row in rows}, {QUEUE_SOURCE_REQUEST})

    def test_metric_card_includes_hover_explanation(self) -> None:
        html = metric("估值安全", 83.36)
        self.assertIn('class="metric metric-signal-ok"', html)
        self.assertIn('tabindex="0"', html)
        self.assertIn('class="metric-tooltip"', html)
        self.assertIn("入口估值安全度", html)
        self.assertIn("估值相对可接受", html)
        self.assertIn("metric-signal-ok", html)
        self.assertIn("估值可接受", html)

    def test_metric_card_signal_classes_reflect_indicator_state(self) -> None:
        self.assertIn("metric-signal-safe", metric("证据质量", 89.25))
        self.assertIn("证据可信", metric("证据质量", 89.25))
        self.assertIn("metric-signal-watch", metric("PB", 3.77))
        self.assertIn("偏贵", metric("PB", 3.77))
        self.assertIn("metric-signal-neutral", metric("收盘", 106.31))

    def test_upstream_signal_uses_myinvestleader_fields(self) -> None:
        row = {
            "code": "688256.SH",
            "name": "寒武纪",
            "theme": "AI算力/通信",
            "themes_json": '["AI算力/通信","硬科技电子/半导体"]',
            "deep_rating": "A",
            "deep_label": "可跟踪龙头",
            "deep_score": 73.10,
            "shadow_observation_eligible": 1,
            "candidate_leader_tier": "证据确认龙头",
            "candidate_leader_claim": "国产AI芯片龙头",
            "candidate_evidence_score": 89.0,
            "candidate_evidence_count": 5,
            "candidate_hard_evidence_count": 4,
            "market_json": '{"r20": 18.5, "r60": 55.0, "turnover_rate": 4.2}',
            "scores_json": '{"theme_binding": 91.0, "evidence_quality": 89.0, "trading_structure": 78.0}',
            "risk_flags_json": '["估值拥挤"]',
            "data_gaps_json": "[]",
            "xueqiu_url": "https://xueqiu.com/S/SH688256",
        }
        signal = upstream_signal_summary(row)
        summary = leader_to_summary(row)
        self.assertEqual(signal["source"], "MyInvestLeader /api/index")
        self.assertEqual(signal["bucket"], "strong")
        self.assertEqual(signal["leader_claim"], "国产AI芯片龙头")
        self.assertEqual(summary["upstream_signal"]["theme_binding"], 91.0)

    def test_theme_context_matches_mainline_and_market_rows(self) -> None:
        payload = {
            "latest_report": {
                "report_id": "mainline_review_2026-06-23_173855",
                "basis_date": "2026-06-23",
                "generated_at": "2026-06-23 17:38:55 CST",
                "data_quality_status": "degraded",
                "contract_validation_status": "pass",
            },
            "mainline_ranking": [
                {
                    "theme_id": "hard_tech_semiconductor",
                    "theme_name": "硬科技电子/半导体",
                    "mainline_score_v6": 0.9642,
                    "lifecycle_state": "accelerating",
                    "lifecycle_state_label": "升温加速",
                    "cycle_stage": "launch_confirmation",
                    "cycle_stage_label": "启动确认期",
                    "cycle_market_score": 53.76,
                    "cycle_evidence_score": 60.16,
                    "cycle_stage_advice": "政策和市场开始同向",
                }
            ],
            "legacy_theme_ranking": [
                {
                    "theme": "硬科技电子/半导体",
                    "market_score": 53.7592,
                    "evidence_score": 60.1583,
                    "policy_score": 96.42,
                    "etf_score": 97.4025,
                    "ths_score": 83.19,
                    "limit_count": 4,
                    "top_etf": "588170.SH 半导体ETF",
                }
            ],
            "market": {
                "breadth": {"up_ratio": 50.1, "r5_positive_ratio": 39.6, "r20_positive_ratio": 20.1},
                "broad_indexes": [{"code": "000688.SH", "name": "科创50", "r5": 9.6, "r20": 1.0}],
            },
        }
        meta = theme_report_meta(payload)
        context = theme_context_for(["硬科技电子/半导体"], payload)
        market = market_context_summary(payload)
        self.assertEqual(meta["report_id"], "mainline_review_2026-06-23_173855")
        self.assertIsNotNone(context)
        self.assertEqual(context["cycle_stage_label"], "启动确认期")
        self.assertEqual(context["bucket"], "strong")
        self.assertEqual(context["etf_score"], 97.4025)
        self.assertEqual(context["crowding_signal"], "热度拥挤代理偏高")
        self.assertEqual(market["risk_appetite"], "结构性中性")

    def test_enriched_leader_item_feeds_theme_context_into_upstream_signal(self) -> None:
        theme_payload = {
            "latest_report": {"report_id": "mainline_review_2026-06-23_173855", "basis_date": "2026-06-23"},
            "mainline_ranking": [
                {
                    "theme_name": "AI算力/通信",
                    "mainline_score_v6": 1.7533,
                    "lifecycle_state_label": "升温加速",
                    "cycle_stage": "policy_incubation",
                    "cycle_stage_label": "政策孕育期",
                    "cycle_market_score": 27.05,
                }
            ],
            "legacy_theme_ranking": [{"theme": "AI算力/通信", "etf_score": 78.98, "market_score": 27.05}],
            "market": {"breadth": {"up_ratio": 50.1}, "broad_indexes": []},
        }
        item = {
            "code": "688256.SH",
            "name": "寒武纪",
            "theme": "AI算力/通信",
            "themes": ["AI算力/通信"],
            "deep_rating": "A",
            "deep_label": "可跟踪龙头",
            "deep_score": 73.1,
            "candidate_leader_claim": "国产AI芯片龙头",
            "scores": {"theme_binding": 91.0, "evidence_quality": 89.0},
            "market": {},
        }
        enriched = enrich_leader_item(item, theme_payload)
        row = {
            "code": item["code"],
            "name": item["name"],
            "theme": item["theme"],
            "themes_json": '["AI算力/通信"]',
            "deep_rating": "A",
            "deep_label": "可跟踪龙头",
            "deep_score": 73.1,
            "shadow_observation_eligible": 1,
            "candidate_leader_tier": "证据确认龙头",
            "candidate_leader_claim": "国产AI芯片龙头",
            "candidate_evidence_score": 89.0,
            "candidate_evidence_count": 5,
            "candidate_hard_evidence_count": 4,
            "market_json": "{}",
            "scores_json": '{"theme_binding": 91.0, "evidence_quality": 89.0}',
            "risk_flags_json": "[]",
            "data_gaps_json": "[]",
            "raw_json": json.dumps(enriched, ensure_ascii=False),
            "xueqiu_url": "https://xueqiu.com/S/SH688256",
        }
        signal = upstream_signal_summary(row)
        self.assertEqual(signal["source"], "MyInvestLeader /api/index + MyInvestTheme /api/index")
        self.assertEqual(signal["bucket"], "watch")
        self.assertEqual(signal["cycle_stage_label"], "政策孕育期")
        self.assertEqual(signal["risk_appetite"], "结构性中性")

    def test_decision_matrix_separates_mainline_from_financial_safety(self) -> None:
        upstream = {
            "bucket": "strong",
            "label": "上游主线信号强",
        }
        financial = {
            "bucket": "low",
            "label": "财务安全边际不足",
        }
        matrix = decision_matrix_summary(upstream, financial)
        self.assertEqual(matrix["posture"], "主线弹性跟踪")
        self.assertIn("不按安全边际重仓", matrix["conclusion"])

    def test_financial_signal_reads_deterministic_valuation_scores(self) -> None:
        row = {
            "valuation_low": 79.6,
            "valuation_mid": 103.0,
            "valuation_high": 126.0,
            "valuation_unit": "CNY/share",
            "valuation_method": "PE+PB+DCF",
            "heavy_position_view": "高估暂缓",
            "raw_json": (
                '{"valuation":{"undervalued_score":0,"growth_score":100,'
                '"quality_score":18.5,"risk_adjusted_score":27.9},'
                '"conclusion":{"summary":"确定性规则评分"}}'
            ),
        }
        signal = financial_signal_summary(row)
        self.assertEqual(signal["bucket"], "low")
        self.assertEqual(signal["raw_grade"], "高估暂缓")
        self.assertEqual(signal["growth_score"], 100.0)

    def test_signal_matrix_section_labels_sources(self) -> None:
        html = render_signal_matrix(
            {
                "theme": "AI算力/通信",
                "label": "上游主线信号强",
                "rating": "A 可跟踪龙头",
                "theme_binding": 91,
                "leader_score": 73.1,
                "evidence_quality": 89,
                "trading_structure": 78,
                "leader_claim": "国产AI芯片龙头",
                "risk_flags": [],
            },
            {
                "label": "财务安全边际不足",
                "source": "MyInvestStock deterministic valuation",
                "undervalued_score": 0,
                "growth_score": 100,
                "quality_score": 18.5,
                "risk_adjusted_score": 27.9,
                "raw_grade": "高估暂缓",
                "valuation_range": {"low": 79.6, "mid": 103, "high": 126},
            },
            {"posture": "主线弹性跟踪", "conclusion": "上游主线强，但财务安全边际不足"},
        )
        self.assertIn("来自 MyInvestLeader", html)
        self.assertIn("估值模型原始标签：高估暂缓", html)
        self.assertIn("主线弹性跟踪", html)

    def test_queue_rows_link_to_stock_page(self) -> None:
        html = render_queue_rows(
            [
                {
                    "priority": 1,
                    "stage": 1,
                    "code": "600519.SH",
                    "name": "贵州茅台",
                    "source_type": QUEUE_SOURCE_TRACKABLE,
                    "trigger_reason": TRIGGER_TRACKABLE_LEADER,
                    "task_type": TASK_TYPE_STOCK_RESEARCH,
                    "status": "pending",
                    "task_keyword": "MyInvestStock 个股深研 600519.SH 贵州茅台",
                }
            ]
        )
        self.assertIn('href="/stocks/600519.SH"', html)
        self.assertIn('href="https://xueqiu.com/S/SH600519"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn(">贵州茅台</a>", html)
        self.assertIn(">可跟踪龙头</td>", html)

    def test_requested_stock_enqueue_marks_queue_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "manual.sqlite"
            result = enqueue_requested_stock("002594.SZ", name="比亚迪", db_path=db_path)
            with closing(connect(db_path)) as conn:
                rows = list_queue(conn)
                report = latest_report(conn)
            self.assertEqual(result["queued"], [TASK_TYPE_STOCK_RESEARCH])
            self.assertEqual({row["source_type"] for row in rows}, {QUEUE_SOURCE_REQUEST})
            self.assertEqual([row["task_type"] for row in rows], [TASK_TYPE_STOCK_RESEARCH])
            self.assertEqual(rows[0]["trigger_reason"], TRIGGER_MANUAL_REQUEST)
            self.assertIsNone(report)

    def test_requested_stock_without_name_resolves_stock_basic_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "manual.sqlite"
            with patch("myinveststock.leader_index.lookup_tushare_stock_name", return_value="平安银行") as lookup:
                result = enqueue_requested_stock("000001.SZ", db_path=db_path)
            with closing(connect(db_path)) as conn:
                rows = list_queue(conn)
            self.assertEqual(result["name"], "平安银行")
            self.assertEqual(rows[0]["name"], "平安银行")
            self.assertIn("MyInvestStock 个股深研 000001.SZ 平安银行", rows[0]["task_keyword"])
            self.assertIn("唯一研究对象：000001.SZ 平安银行", rows[0]["prompt"])
            lookup.assert_called_once_with("000001.SZ")

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
        self.assertIn("参考价格区间随时间变化图", html)
        self.assertIn("<h2>参考价格区间历史</h2>", html)
        self.assertIn("valuation-band", html)
        self.assertIn("valuation-mid-line", html)
        self.assertIn("价格 CNY/share", html)
        self.assertIn("06-22", html)
        self.assertIn("06-24", html)

    def test_valuation_chart_uses_close_price_overlay_when_prices_exist(self) -> None:
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
        self.assertIn("收盘价折线叠加参考价格区间图", html)
        self.assertIn("close-price-layer", html)
        self.assertIn("close-price-line", html)
        self.assertIn("current-price-line", html)
        self.assertIn('class="current-price-line" x1="0.0"', html)
        self.assertIn("current-price-label", html)
        self.assertIn("当前价 112.00", html)
        self.assertIn("valuation-reference-layer", html)
        self.assertIn('class="valuation-reference-line valuation-reference-line-low" x1="0.0"', html)
        self.assertIn("保守 100.00", html)
        self.assertIn("合理 130.00", html)
        self.assertIn("乐观 160.00", html)
        self.assertIn("valuation-step-band", html)
        self.assertIn("legend-close", html)
        self.assertIn("legend-current", html)
        self.assertIn("legend-reference", html)
        self.assertIn("2024-09-24以来收盘价", html)
        self.assertIn("个股深研刷新点", html)
        self.assertNotIn("kline-layer", html)

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

    def test_valuation_details_show_component_formulas_and_links(self) -> None:
        latest = {
            "raw_json": json.dumps(
                {
                    "valuation": {
                        "method": "PE+PB+DCF",
                        "pe": 15.76,
                        "pb": 3.77,
                        "peg": 0.15,
                        "undervalued_score": 100.0,
                        "risk_adjusted_score": 86.58,
                        "key_assumptions": ["deterministic engine"],
                        "calculation": {
                            "combined_formula": "final low/mid/high = weighted average of PE, PB and DCF",
                            "components": [
                                {
                                    "method": "PE",
                                    "weight": 0.4,
                                    "intrinsic_value_low": 100.0,
                                    "intrinsic_value_mid": 125.0,
                                    "intrinsic_value_high": 150.0,
                                    "formula": "PE mid = EPS × adjusted_pe",
                                    "inputs": ["eps=5", "adjusted_pe=25"],
                                },
                                {
                                    "method": "PB",
                                    "weight": 0.3,
                                    "intrinsic_value_low": 80.0,
                                    "intrinsic_value_mid": 100.0,
                                    "intrinsic_value_high": 120.0,
                                    "formula": "PB mid = book_value_per_share × adjusted_pb",
                                    "inputs": ["book_value_per_share=20", "adjusted_pb=5"],
                                },
                                {
                                    "method": "DCF",
                                    "weight": 0.3,
                                    "intrinsic_value_low": 120.0,
                                    "intrinsic_value_mid": 160.0,
                                    "intrinsic_value_high": 220.0,
                                    "formula": "DCF = next_fcf / spread",
                                    "inputs": ["fcf_per_share=4"],
                                },
                            ],
                        },
                    },
                    "fundamentals": {"revenue_growth": 0.05, "profit_growth": 0.3, "roe": 0.21, "debt_ratio": 0.29},
                    "peer_comparison": {
                        "relative_valuation": "stock PE 15.76; industry median PE 41.82.",
                        "competitive_position": "stock ROE 20.98%; industry median ROE 4.27%.",
                    },
                },
                ensure_ascii=False,
            ),
            "valuation_method": "PE+PB+DCF",
        }
        html = render_valuation_details(latest)
        self.assertIn("估值依据与计算口径", html)
        self.assertIn("PE mid = EPS", html)
        self.assertIn("PB mid = book_value_per_share", html)
        self.assertIn("DCF = next_fcf", html)
        self.assertIn("<td>0.40</td>", html)
        self.assertIn("stock PE 15.76", html)

        links = render_report_links("603259.SH", latest)
        self.assertIn("/api/stocks/603259.SH/research/latest/raw", links)
        self.assertIn("/docs/RESEARCH_SCHEMA.md", links)
        self.assertIn("/docs/AUTOMATION.md", links)

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

    def test_price_refresh_subjects_include_queue_runs_and_history(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "subjects.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                upsert_report(
                    conn,
                    report_id="leader_review_2026-06-23",
                    schema_version="leader.v1",
                    generated_at="2026-06-23T18:50:00+08:00",
                    basis_date="2026-06-23",
                    theme_report_id=None,
                    source_url="https://leader.okbbc.com/api/index",
                    fetched_at="2026-06-23T11:00:00+00:00",
                    raw_path=None,
                )
                upsert_report(
                    conn,
                    report_id="leader_review_2026-06-24",
                    schema_version="leader.v1",
                    generated_at="2026-06-24T18:50:00+08:00",
                    basis_date="2026-06-24",
                    theme_report_id=None,
                    source_url="https://leader.okbbc.com/api/index",
                    fetched_at="2026-06-24T11:00:00+00:00",
                    raw_path=None,
                )
                upsert_trackable_leader(
                    conn,
                    report_id="leader_review_2026-06-23",
                    item={"code": "688256.SH", "name": "寒武纪"},
                    created_at="2026-06-23T11:00:00+00:00",
                )
                upsert_trackable_leader(
                    conn,
                    report_id="leader_review_2026-06-24",
                    item={"code": "603259.SH", "name": "药明康德"},
                    created_at="2026-06-24T11:00:00+00:00",
                )
                upsert_queue_item(
                    conn,
                    report_id="leader_review_2026-06-24",
                    code="002594.SZ",
                    name="比亚迪",
                    priority=1,
                    stage=1,
                    task_type=TASK_TYPE_STOCK_RESEARCH,
                    task_keyword="MyInvestStock 个股深研 002594.SZ 比亚迪",
                    prompt="研究提示词",
                    depends_on_task_type=None,
                    trigger_reason=TRIGGER_MANUAL_REQUEST,
                    task_date="2026-06-24",
                    now="2026-06-24T11:00:00+00:00",
                    source_type=QUEUE_SOURCE_REQUEST,
                )
                conn.execute(
                    """
                    INSERT INTO stock_research_runs (
                        code, name, task_type, research_date, created_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    ("300750.SZ", "宁德时代", TASK_TYPE_STOCK_RESEARCH, "2026-06-24", "2026-06-24T11:00:00+00:00", "complete"),
                )
                rows = list_price_refresh_subjects(conn)

            subjects = {row["code"]: dict(row) for row in rows}
            self.assertEqual(list(subjects), ["603259.SH", "002594.SZ", "300750.SZ", "688256.SH"])
            self.assertIn("latest_trackable", subjects["603259.SH"]["sources"])
            self.assertIn("queue", subjects["002594.SZ"]["sources"])
            self.assertIn("research", subjects["300750.SZ"]["sources"])
            self.assertIn("trackable_history", subjects["688256.SH"]["sources"])

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
        self.assertEqual(summary["links"]["research_gateway"], "/research?stock=603259.SH")
        self.assertEqual(summary["market"]["close"], 106.83)
        self.assertIn("upstream_signal", summary)

    def test_latest_research_summary_contract(self) -> None:
        row = {
            "id": 1,
            "task_type": TASK_TYPE_STOCK_RESEARCH,
            "trigger_reason": TRIGGER_TRACKABLE_LEADER,
            "research_date": "2026-06-24",
            "status": "complete",
            "title": "个股深研",
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
            "raw_json": '{"valuation":{"undervalued_score":75,"growth_score":60,"quality_score":70,"risk_adjusted_score":66}}',
        }
        summary = research_run_to_summary(row)
        self.assertEqual(summary["task_type"], TASK_TYPE_STOCK_RESEARCH)
        self.assertEqual(summary["trigger_reason"], TRIGGER_TRACKABLE_LEADER)
        self.assertEqual(summary["valuation"]["mid"], 120)
        self.assertEqual(summary["risks"], ["估值收缩"])
        self.assertEqual(summary["financial_signal"]["bucket"], "high")


if __name__ == "__main__":
    unittest.main()
