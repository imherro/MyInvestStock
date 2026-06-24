from __future__ import annotations

import json
import re
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DB_PATH, LEADER_INDEX_URL, RAW_DATA_DIR
from .db import (
    QUEUE_SOURCE_REQUEST,
    connect,
    has_strategic_work,
    init_db,
    upsert_queue_item,
    upsert_report,
    upsert_trackable_leader,
    utc_now,
)

STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


STOCK_REPORT_SCHEMA_INSTRUCTION = """StockResearchReport 结构化输出要求：
- 最终只输出一个 JSON object，不要输出 Markdown 包裹。
- JSON 必须符合 core/schema/stock_report.py 中 StockResearchReport。
- 顶层字段固定为：schema_version, report_version, report_hash, run_id, stock_code, stock_name, source_report_id, task_type, research_date, status, title, summary, industry_position, competition_landscape, upstream_downstream, annual_growth, multi_bagger_potential, heavy_position_view, fundamentals, valuation, peer_comparison, risk, conclusion, evidence, assumptions。
- 禁止输出 schema 以外的额外字段；禁止把未定义内容塞进自由 dict。
- stock_code 使用唯一研究对象代码，stock_name 使用唯一研究对象名称，source_report_id 使用入口 report_id。
- research_date 必须使用入口 basis_date。
- run_id 必须等于 hash(stock_code + task_type + research_date + schema_version)，可省略让导入端自动生成；如果提供错误 run_id 会被拒绝。
- fundamentals 必须包含 revenue_growth, profit_growth, roe, debt_ratio, revenue_quality, profit_quality, cash_flow_quality, balance_sheet_quality。
- valuation 必须包含 pe, pb, peg, intrinsic_value_low, intrinsic_value_mid, intrinsic_value_high, unit, method, confidence, key_assumptions；可包含 engine_version, undervalued_score, growth_score, quality_score, risk_adjusted_score。
- peer_comparison 必须包含 industry_rank, competitors, relative_valuation, competitive_position。
- risk 必须包含 financial_risk, industry_risk, sentiment_risk, invalidation_conditions。
- conclusion 必须包含 grade, confidence, summary；grade 必须等于 heavy_position_view。
- evidence 是对象数组，每项必须包含 source, date, url, purpose, detail。
- assumptions 是字符串数组。
- heavy_position_view/grade 只能是：不具备、观察、可跟踪、核心仓研究资格、高估暂缓。
- status 只能是 complete、draft、blocked；confidence 只能是 low、medium、high。"""


FINANCIAL_ASSEMBLY_INPUT_INSTRUCTION = """financial assembly_input 结构化要求：
- 你的角色是财务结构化输入构建器，不是估值师，也不是最终报告生成器。
- 不要重新判断主线强弱、ETF 趋势或行业热度；这些信号只引用 MyInvestLeader /api/index 已入库的上游信号。
- MyInvestStock 的财务估值只输出财务安全边际，不用财务高估一票否决上游主线跟踪价值。
- 不要手写最终 StockResearchReport；最终报告必须由 scripts/build_research_report.py 或 core/report.build_stock_report(...) 生成。
- 不要重新计算估值区间，不要修改 deterministic engine 产出的 valuation、peer_comparison、risk、conclusion、report_hash。
- assembly_input 必须是一个 JSON object，至少包含 stock_code, stock_name, source_report_id, task_type, research_date, financial_rows, valuation_inputs, peers, risk_signals。
- task_type 固定为 financial；research_date 使用入口 basis_date。
- financial_rows 只放结构化财务输入，例如 revenue, net_profit, equity, gross_margin, debt, free_cash_flow, market_cap；缺失字段必须显式说明到 evidence 或 assumptions，不要编造。
- valuation_inputs 只放估值模型输入，例如 current_price, stock_pe/pe, pb, eps, book_value_per_share, industry_pb, fcf_per_share, weights；这些是模型输入，不是最终估值结论。
- peers 只放同业样本输入，例如 stock_code, pe, roe；同业选择口径写入 assumptions 或 evidence。
- risk_signals 只放可解释风险输入，例如 financial_risk, industry_risk, sentiment_risk, invalidation_conditions。
- 可包含 title, summary, industry_position, competition_landscape, upstream_downstream, annual_growth, multi_bagger_potential, evidence, assumptions 作为解释性输入，但这些字段不能覆盖 deterministic valuation 或 conclusion，也不能覆盖 MyInvestLeader 上游主线信号。
- 所有来源必须进入 evidence：source, date, url, purpose, detail。
- 禁止输出交易指令、现金金额、股数或买卖建议。"""


REPORT_EXPLAINER_INSTRUCTION = """你是 A 股研究报告解释器。

输入是已经通过 schema 校验的 StockResearchReport。你的任务是解释，不是计算。

禁止：
- 不得修改任何数值、估值区间、signal、grade、report_hash 或 run_id。
- 不得重新估值。
- 不得引入新外部数据。
- 不得给出新的买卖建议、现金金额或股数。
- 不得用自己的判断覆盖系统结论。

输出只做五件事：
1. 解释核心财务状态。
2. 解释已有估值区间和 signal 的含义。
3. 解释 risk 字段中的主要风险来源。
4. 解释系统为什么给出当前 heavy_position_view / conclusion.grade。
5. 用通俗语言总结结论。"""


def fetch_index(url: str = LEADER_INDEX_URL, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MyInvestStock/0.1 (+local research workbench)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read().decode("utf-8")
    return json.loads(data)


def primary_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    key_results = payload.get("key_results") or {}
    primary = key_results.get("primary_output") or {}
    items = primary.get("items") or []
    if not isinstance(items, list):
        raise ValueError("key_results.primary_output.items is not a list")
    clean_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        name = str(item.get("name") or "")
        if STOCK_CODE_RE.match(code) and name:
            clean_items.append(item)
    return clean_items


def report_meta(payload: dict[str, Any]) -> dict[str, Any]:
    report = payload.get("report") or {}
    report_id = report.get("report_id") or payload.get("report_id")
    if not report_id:
        raise ValueError("Missing report.report_id")
    return {
        "report_id": report_id,
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "basis_date": report.get("basis_date"),
        "theme_report_id": report.get("theme_report_id"),
    }


def save_raw_payload(payload: dict[str, Any], report_id: str, raw_dir: Path = RAW_DATA_DIR) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_report_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", report_id)
    path = raw_dir / f"{safe_report_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def build_strategic_prompt(item: dict[str, Any], report: dict[str, Any]) -> str:
    code = item["code"]
    name = item["name"]
    theme = item.get("theme") or ""
    report_id = report["report_id"]
    basis_date = report.get("basis_date") or ""
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股战略深研。

唯一研究对象：{code} {name}。

入口信息：
- 数据入口：https://leader.okbbc.com/api/index
- 入口路径：key_results.primary_output.items
- report_id：{report_id}
- basis_date：{basis_date}
- 主题：{theme}

硬约束：
- 只研究这一只股票，禁止同时研究其他 A可跟踪龙头。
- 先读取 /api/index，只使用 key_results.primary_output.items 中匹配 {code} 的记录作为入口。
- 可使用 stock_deep_research.stocks 中匹配 {code} 的记录作为已有基础材料。
- 主线、ETF、行业热度和龙头确认只引用 MyInvestLeader /api/index 入库信号，不重新研究主线是否成立。
- 本任务只做战略、行业、竞争和长期潜力研究，不给最终估值区间。
- 网络资料可作补充证据，但要记录来源和日期。
- 不输出交易指令、不输出现金金额、不输出股数。

必须覆盖：
- 行业位置：公司所处细分赛道、产业链环节、国内/全球地位。
- 市场空间：当前空间、未来 3-5 年扩容逻辑、政策或技术驱动。
- 竞争格局：直接竞争者、替代者、潜在进入者、行业集中度变化。
- 上下游公司：关键供应商、客户、平台、渠道和议价关系。
- 战略壁垒：技术、品牌、渠道、成本、客户锁定、牌照或生态。
- 五倍/十倍潜力：只判断战略条件，不用股价目标代替逻辑。
- 战略证伪条件：什么行业或竞争数据出现后说明长期逻辑错了。

战略深研是长期底稿，默认只做一次；除非公司业务结构、行业格局或长期逻辑发生断层变化，不要每日重复生成。

战略深研 schema 规则：
- task_type 必须为 strategic。
- valuation.intrinsic_value_low / intrinsic_value_mid / intrinsic_value_high 必须为 null。
- valuation.method 写“strategic-only”，valuation.confidence 写 low/medium/high。

{STOCK_REPORT_SCHEMA_INSTRUCTION}

完成后将 task_type='strategic' 的结构化结果写入 stock_research_runs。"""


def build_financial_prompt(item: dict[str, Any], report: dict[str, Any]) -> str:
    code = item["code"]
    name = item["name"]
    theme = item.get("theme") or ""
    report_id = report["report_id"]
    basis_date = report.get("basis_date") or ""
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股财务估值深研输入构建。

唯一研究对象：{code} {name}。

入口信息：
- 数据入口：https://leader.okbbc.com/api/index
- 入口路径：key_results.primary_output.items
- report_id：{report_id}
- basis_date：{basis_date}
- 主题：{theme}

前置依赖：
- 先读取本地 stock_research_runs 中 {code} 的 task_type='strategic' 最新记录。
- 如果战略深研不存在，先停止并把本任务标记为 blocked，不要跳过前置依赖。

硬约束：
- 只研究这一只股票，禁止同时研究其他 A可跟踪龙头。
- Tushare 是 A 股财务、行情、估值结构化主源。
- 网络资料只用于补充财务口径、行业数据或管理层表述。
- 主线、ETF、行业热度和龙头确认只引用 MyInvestLeader /api/index 已入库信号，不重新判断主线强弱。
- 本任务可以多次重复执行，用最新财务、估值和价格数据刷新结论。
- 本任务只构建 deterministic report 所需的 assembly_input，不直接生成最终 StockResearchReport。
- LLM 只能负责搜集、清洗、归一化输入和解释脚本输出；不能重新计算估值，不能给出新的 grade。
- 不输出交易指令、不输出现金金额、不输出股数。

必须构建 assembly_input：
- 财务输入：收入、利润、毛利率、ROE、现金流、负债、market_cap 等结构化字段。
- 估值输入：current_price, stock_pe/pe, pb, eps, book_value_per_share, industry_pb, fcf_per_share, weights 等模型输入。
- 同业输入：同业 stock_code、pe、roe 和样本选择理由。
- 风险输入：financial_risk、industry_risk、sentiment_risk、invalidation_conditions。
- 解释性输入：行业地位、竞争格局、上下游、年增长率、五倍/十倍潜力校验可以作为文字输入，但不允许覆盖系统最终结论。

执行流程：
1. 收集 Tushare 和必要网络补充资料，形成 assembly_input JSON。
2. 将 assembly_input 写入 temp/assembly_inputs/{code}_financial_{basis_date}.json。
3. 运行 python scripts/build_research_report.py --audit-db data/local/myinveststock.sqlite temp/assembly_inputs/{code}_financial_{basis_date}.json > temp/reports/{code}_financial_{basis_date}.json。
4. 用 python scripts/import_research_run.py temp/reports/{code}_financial_{basis_date}.json 入库。
5. 导入成功后，汇报 run_id、report_hash、audit_log stage 覆盖、verify_run 结果和系统生成的主要结论摘要。

{FINANCIAL_ASSEMBLY_INPUT_INSTRUCTION}

完成后保证 /stocks/{code} 能看到由 deterministic pipeline 生成并入库的估值区间历史叠加。"""


def build_requested_strategic_prompt(item: dict[str, Any], report: dict[str, Any]) -> str:
    code = item["code"]
    name = item["name"]
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股战略深研。

唯一研究对象：{code} {name}。

入口信息：
- 入口来源：用户主动请求 /research?stock={code}
- report_id：{report["report_id"]}
- basis_date：{report.get("basis_date")}
- 主题：其他请求

硬约束：
- 这只股票不要求出现在 /api/index 的 A可跟踪龙头列表里。
- 只研究这一只股票，禁止同时研究其他股票。
- Tushare 是 A 股结构化主源，网络资料只作补充证据。
- 如果本地已有 MyInvestLeader 上游信号，只引用其主线结论，不重新判断主线强弱。
- 本任务只做战略、行业、竞争和长期潜力研究，不给最终估值区间。
- 不输出交易指令、不输出现金金额、不输出股数。

必须覆盖：
- 行业位置：公司所处细分赛道、产业链环节、国内/全球地位。
- 市场空间：当前空间、未来 3-5 年扩容逻辑、政策或技术驱动。
- 竞争格局：直接竞争者、替代者、潜在进入者、行业集中度变化。
- 上下游公司：关键供应商、客户、平台、渠道和议价关系。
- 战略壁垒：技术、品牌、渠道、成本、客户锁定、牌照或生态。
- 五倍/十倍潜力：只判断战略条件，不用股价目标代替逻辑。
- 战略证伪条件：什么行业或竞争数据出现后说明长期逻辑错了。

完成后输出结构化 JSON，并通过 `scripts/import_research_run.py` 入库为 task_type='strategic'。
战略 JSON 不允许写估值区间字段。
{STOCK_REPORT_SCHEMA_INSTRUCTION}
JSON 必须符合 StockResearchReport：`task_type` 为 `strategic`，`valuation.intrinsic_value_low/mid/high` 均为 `null`，禁止额外字段。"""


def build_requested_financial_prompt(item: dict[str, Any], report: dict[str, Any]) -> str:
    code = item["code"]
    name = item["name"]
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股财务估值深研输入构建。

唯一研究对象：{code} {name}。

入口信息：
- 入口来源：用户主动请求 /research?stock={code}
- report_id：{report["report_id"]}
- basis_date：{report.get("basis_date")}
- 主题：其他请求

前置依赖：
- 先读取本地 stock_research_runs 中 {code} 的 task_type='strategic' 最新记录。
- 如果战略深研不存在，先停止并把本任务标记为 blocked，不要跳过前置依赖。

硬约束：
- 这只股票不要求出现在 /api/index 的 A可跟踪龙头列表里。
- 只研究这一只股票，禁止同时研究其他股票。
- Tushare 是 A 股财务、行情、估值结构化主源。
- 网络资料只用于补充财务口径、行业数据或管理层表述。
- 如果本地已有 MyInvestLeader 上游信号，只引用其主线结论，不重新判断主线强弱。
- 本任务可以多次重复执行，用最新财务、估值和价格数据刷新结论。
- 本任务只构建 deterministic report 所需的 assembly_input，不直接生成最终 StockResearchReport。
- 不输出交易指令、不输出现金金额、不输出股数。

{FINANCIAL_ASSEMBLY_INPUT_INSTRUCTION}

完成后保证 /stocks/{code} 能看到由 deterministic pipeline 生成并入库的估值区间历史叠加。"""


def build_report_explainer_prompt(report_output: dict[str, Any] | str) -> str:
    report_text = (
        report_output
        if isinstance(report_output, str)
        else json.dumps(report_output, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return f"""{REPORT_EXPLAINER_INSTRUCTION}

StockResearchReport:
{report_text}
"""


def enqueue_requested_stock(
    code: str,
    *,
    name: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    stock_code = code.strip().upper()
    if not STOCK_CODE_RE.match(stock_code):
        raise ValueError(f"invalid stock code: {code}")
    stock_name = (name or stock_code).strip() or stock_code
    basis_date = datetime.now().date().isoformat()
    report = {
        "report_id": f"manual_research_request_{basis_date}",
        "schema_version": "manual_research_request.v1",
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "basis_date": basis_date,
        "theme_report_id": None,
    }
    item = {"code": stock_code, "name": stock_name, "theme": "其他请求"}
    now = utc_now()
    db_target = Path(db_path) if db_path is not None else DB_PATH
    init_db(db_target)
    queued: list[str] = []
    with closing(connect(db_target)) as conn:
        upsert_report(
            conn,
            report_id=report["report_id"],
            schema_version=report["schema_version"],
            generated_at=report["generated_at"],
            basis_date=report["basis_date"],
            theme_report_id=None,
            source_url=f"/research?stock={stock_code}",
            fetched_at=now,
            raw_path=None,
        )
        if not has_strategic_work(conn, stock_code):
            upsert_queue_item(
                conn,
                report_id=report["report_id"],
                code=stock_code,
                name=stock_name,
                priority=900,
                stage=1,
                task_type="strategic",
                task_keyword=f"MyInvestStock 个股战略深研 {stock_code} {stock_name}",
                prompt=build_requested_strategic_prompt(item, report),
                depends_on_task_type=None,
                task_date=basis_date,
                now=now,
                source_type=QUEUE_SOURCE_REQUEST,
                source_detail="/research",
            )
            queued.append("strategic")
        upsert_queue_item(
            conn,
            report_id=report["report_id"],
            code=stock_code,
            name=stock_name,
            priority=900,
            stage=2,
            task_type="financial",
            task_keyword=f"MyInvestStock 个股财务估值深研 {stock_code} {stock_name}",
            prompt=build_requested_financial_prompt(item, report),
            depends_on_task_type="strategic",
            task_date=basis_date,
            now=now,
            source_type=QUEUE_SOURCE_REQUEST,
            source_detail="/research",
        )
        queued.append("financial")
        conn.commit()
    return {
        "code": stock_code,
        "name": stock_name,
        "report_id": report["report_id"],
        "basis_date": basis_date,
        "queued": queued,
    }


def ingest_payload(
    payload: dict[str, Any],
    *,
    source_url: str = LEADER_INDEX_URL,
    raw_path: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    db_target = Path(db_path) if db_path is not None else DB_PATH
    init_db(db_target)
    report = report_meta(payload)
    items = primary_items(payload)
    now = utc_now()
    with closing(connect(db_target)) as conn:
        upsert_report(
            conn,
            report_id=report["report_id"],
            schema_version=report.get("schema_version"),
            generated_at=report.get("generated_at"),
            basis_date=report.get("basis_date"),
            theme_report_id=report.get("theme_report_id"),
            source_url=source_url,
            fetched_at=now,
            raw_path=raw_path,
        )
        for priority, item in enumerate(
            sorted(items, key=lambda row: row.get("deep_score") or 0, reverse=True),
            start=1,
        ):
            upsert_trackable_leader(conn, report_id=report["report_id"], item=item, created_at=now)
            if not has_strategic_work(conn, item["code"]):
                upsert_queue_item(
                    conn,
                    report_id=report["report_id"],
                    code=item["code"],
                    name=item["name"],
                    priority=priority,
                    stage=1,
                    task_type="strategic",
                    task_keyword=f"MyInvestStock 个股战略深研 {item['code']} {item['name']}",
                    prompt=build_strategic_prompt(item, report),
                    depends_on_task_type=None,
                    task_date=report.get("basis_date"),
                    now=now,
                )
            upsert_queue_item(
                conn,
                report_id=report["report_id"],
                code=item["code"],
                name=item["name"],
                priority=priority,
                stage=2,
                task_type="financial",
                task_keyword=f"MyInvestStock 个股财务估值深研 {item['code']} {item['name']}",
                prompt=build_financial_prompt(item, report),
                depends_on_task_type="strategic",
                task_date=report.get("basis_date"),
                now=now,
            )
        conn.commit()
    return {
        "report_id": report["report_id"],
        "basis_date": report.get("basis_date"),
        "count": len(items),
        "codes": [item["code"] for item in items],
        "names": [item["name"] for item in items],
    }
