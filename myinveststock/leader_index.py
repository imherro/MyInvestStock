from __future__ import annotations

import json
import os
import re
import urllib.request
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DB_PATH, LEADER_INDEX_URL, RAW_DATA_DIR, THEME_INDEX_URL
from .db import (
    QUEUE_SOURCE_REQUEST,
    TASK_TYPE_STOCK_RESEARCH,
    TRIGGER_MANUAL_REQUEST,
    TRIGGER_TRACKABLE_LEADER,
    connect,
    has_stock_research_work,
    init_db,
    upsert_queue_item,
    upsert_report,
    upsert_trackable_leader,
    utc_now,
)
from .theme_index import enrich_leader_item, theme_report_meta

STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
TUSHARE_API_URL = "https://api.tushare.pro"


STOCK_REPORT_SCHEMA_INSTRUCTION = """StockResearchReport 结构化输出要求：
- 最终只输出一个 JSON object，不要输出 Markdown 包裹。
- JSON 必须符合 core/schema/stock_report.py 中 StockResearchReport。
- 顶层字段固定为：schema_version, report_version, report_hash, run_id, stock_code, stock_name, source_report_id, task_type, research_date, trigger_reason, status, title, summary, industry_position, competition_landscape, upstream_downstream, annual_growth, multi_bagger_potential, heavy_position_view, fundamentals, valuation, peer_comparison, risk, conclusion, evidence, assumptions。
- 禁止输出 schema 以外的额外字段；禁止把未定义内容塞进自由 dict。
- stock_code 使用唯一研究对象代码，stock_name 使用唯一研究对象名称，source_report_id 使用入口 report_id。
- task_type 固定为 stock_research。
- research_date 必须使用入口 basis_date。
- trigger_reason 写明本次入队原因。
- run_id 必须等于 hash(stock_code + task_type + research_date + schema_version)，可省略让导入端自动生成；如果提供错误 run_id 会被拒绝。
- fundamentals 必须包含 revenue_growth, profit_growth, roe, debt_ratio, revenue_quality, profit_quality, cash_flow_quality, balance_sheet_quality。
- valuation 必须包含 pe, pb, peg, intrinsic_value_low, intrinsic_value_mid, intrinsic_value_high, unit, method, confidence, key_assumptions；可包含 engine_version, undervalued_score, growth_score, quality_score, risk_adjusted_score。
- peer_comparison 必须包含 industry_rank, competitors, relative_valuation, competitive_position。
- risk 必须包含 financial_risk, industry_risk, sentiment_risk, invalidation_conditions。
- conclusion 必须包含 grade, confidence, summary；grade 必须等于 heavy_position_view。
- evidence 是对象数组，每项必须包含 source, date, url, purpose, detail。
- assumptions 是字符串数组。
- multi_bagger_potential 字段展示为“数倍潜力”，不使用固定倍数目标口号。
- heavy_position_view/grade 只能是：不具备、观察、可跟踪、核心仓研究资格、高估暂缓。
- status 只能是 complete、draft、blocked；confidence 只能是 low、medium、high。"""


STOCK_RESEARCH_INPUT_INSTRUCTION = """stock_research assembly_input 结构化要求：
- 你的角色是完整个股深研输入构建器，不是估值师，也不是最终报告生成器。
- 不要重新判断主线强弱、ETF 趋势、行业热度、拥挤度或风险偏好；这些信号只引用 MyInvestLeader /api/index 个股入口和 MyInvestTheme /api/index 主线环境快照。
- MyInvestStock 的财务估值只输出财务安全边际，不用财务高估一票否决上游主线跟踪价值。
- 不要手写最终 StockResearchReport；最终报告必须由 scripts/build_research_report.py 或 core/report.build_stock_report(...) 生成。
- 不要重新计算估值区间，不要修改 deterministic engine 产出的 valuation、peer_comparison、risk、conclusion、report_hash。
- assembly_input 必须是一个 JSON object，至少包含 stock_code, stock_name, source_report_id, task_type, research_date, trigger_reason, financial_rows, valuation_inputs, peers, risk_signals。
- task_type 固定为 stock_research；research_date 使用入口 basis_date。
- financial_rows 只放结构化财务输入，例如 revenue, net_profit, equity, gross_margin, debt, free_cash_flow, market_cap；缺失字段必须显式说明到 evidence 或 assumptions，不要编造。
- valuation_inputs 只放估值模型输入，例如 current_price, stock_pe/pe, pb, eps, book_value_per_share, industry_pb, fcf_per_share, weights；这些是模型输入，不是最终估值结论。
- peers 只放同业样本输入，例如 stock_code, pe, roe；同业选择口径写入 assumptions 或 evidence。
- risk_signals 只放可解释风险输入，例如 financial_risk, industry_risk, sentiment_risk, invalidation_conditions。
- 必须包含 title, summary, industry_position, competition_landscape, upstream_downstream, annual_growth, multi_bagger_potential, evidence, assumptions 作为完整个股深研解释性输入，但这些字段不能覆盖 deterministic valuation 或 conclusion，也不能覆盖 MyInvestLeader/MyInvestTheme 上游信号。
- multi_bagger_potential 只讨论“数倍潜力”的成立条件、约束条件和证伪条件，不使用固定倍数目标口号。
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


def _clean_requested_stock_name(name: str | None, code: str) -> str | None:
    clean = (name or "").strip()
    if not clean or clean.upper() == code or STOCK_CODE_RE.match(clean.upper()):
        return None
    return clean


def _load_env_value(key: str) -> str | None:
    value = os.environ.get(key)
    if value:
        return value.strip()
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        env_key, env_value = clean.split("=", 1)
        if env_key.strip() == key:
            return env_value.strip().strip('"').strip("'") or None
    return None


def lookup_tushare_stock_name(code: str, *, timeout: int = 10) -> str | None:
    token = _load_env_value("TUSHARE_TOKEN")
    if not token:
        return None
    payload = {
        "api_name": "stock_basic",
        "token": token,
        "params": {"ts_code": code},
        "fields": "ts_code,name",
    }
    request = urllib.request.Request(
        TUSHARE_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MyInvestStock/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, TimeoutError):
        return None
    fields = raw.get("data", {}).get("fields") or []
    items = raw.get("data", {}).get("items") or []
    if "name" not in fields or not items:
        return None
    name_index = fields.index("name")
    first = items[0]
    if not isinstance(first, list) or name_index >= len(first):
        return None
    stock_name = str(first[name_index] or "").strip()
    return stock_name or None


def known_stock_name(conn: Any, code: str) -> str | None:
    row = conn.execute(
        """
        WITH candidates AS (
            SELECT name, 1 AS priority
            FROM trackable_leaders
            WHERE code = ?

            UNION ALL

            SELECT name, 2 AS priority
            FROM stock_research_runs
            WHERE code = ?

            UNION ALL

            SELECT name, 3 AS priority
            FROM research_queue
            WHERE code = ?
        )
        SELECT name
        FROM candidates
        WHERE COALESCE(name, '') != ''
          AND UPPER(name) != ?
        ORDER BY priority ASC
        LIMIT 1
        """,
        (code, code, code, code),
    ).fetchone()
    if row is None:
        return None
    return str(row["name"]).strip() or None


def resolve_requested_stock_name(
    code: str,
    *,
    requested_name: str | None = None,
    db_path: Path | str | None = None,
) -> str:
    stock_code = code.strip().upper()
    clean_requested = _clean_requested_stock_name(requested_name, stock_code)
    if clean_requested:
        return clean_requested
    db_target = Path(db_path) if db_path is not None else DB_PATH
    with closing(connect(db_target)) as conn:
        known = known_stock_name(conn, stock_code)
    if known:
        return known
    return lookup_tushare_stock_name(stock_code) or stock_code


def build_stock_research_prompt(
    item: dict[str, Any],
    report: dict[str, Any],
    *,
    trigger_reason: str,
) -> str:
    code = item["code"]
    name = item["name"]
    theme = item.get("theme") or ""
    theme_context = item.get("theme_context") or {}
    report_id = report["report_id"]
    basis_date = report.get("basis_date") or ""
    theme_report_id = theme_context.get("report_id") or report.get("theme_report_id") or "待读取"
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股完整深研。

唯一研究对象：{code} {name}。
本次触发原因：{trigger_reason}。

入口信息：
- 个股入口：https://leader.okbbc.com/api/index
- 个股入口路径：key_results.primary_output.items
- 主线环境入口：https://theme.okbbc.com/api/index
- report_id：{report_id}
- theme_report_id：{theme_report_id}
- basis_date：{basis_date}
- 主题：{theme}

前置依赖：
- 先读取本地 stock_research_runs 中 {code} 的 task_type='stock_research' 最新记录。
- 如果已有上一版报告，本次必须说明相对上一版哪些结论改变、哪些结论没有改变。

硬约束：
- 只研究这一只股票，禁止同时研究其他 A可跟踪龙头。
- Tushare 是 A 股财务、行情、估值结构化主源。
- 网络资料只用于补充财务口径、行业数据、竞争格局或管理层表述。
- 龙头确认只引用 MyInvestLeader /api/index 入库信号；主线强度、生命周期、周期阶段、ETF/板块趋势、拥挤和风险偏好只引用 MyInvestTheme /api/index 入库信号。
- 不重新判断主线强弱，不用网络资料覆盖 Theme 的主线环境；财务结论必须区分“主线跟踪价值”和“财务安全边际”。
- 每次触发都输出完整个股深研，不再拆分任务类型。
- 本任务只构建 deterministic report 所需的 assembly_input；最终 StockResearchReport 必须由 core/report.build_stock_report(...) 或 scripts/build_research_report.py 生成。
- LLM 只能负责搜集、清洗、归一化输入和解释脚本输出；不能重新计算估值，不能给出新的 grade。
- 不输出交易指令、不输出现金金额、不输出股数。

必须构建 assembly_input：
- 财务输入：收入、利润、毛利率、ROE、现金流、负债、market_cap 等结构化字段。
- 估值输入：current_price, stock_pe/pe, pb, eps, book_value_per_share, industry_pb, fcf_per_share, weights 等模型输入。
- 同业输入：同业 stock_code、pe、roe 和样本选择理由。
- 风险输入：financial_risk、industry_risk、sentiment_risk、invalidation_conditions。
- 解释性输入：行业地位、竞争格局、上下游、年增长率、数倍潜力校验、本次变化对比可以作为文字输入，但不允许覆盖系统最终结论。

执行流程：
1. 收集 Tushare 和必要网络补充资料，形成 assembly_input JSON。
2. assembly_input.task_type 固定为 stock_research，assembly_input.trigger_reason 固定为“{trigger_reason}”。
3. 将 assembly_input 写入 temp/assembly_inputs/{code}_stock_research_{basis_date}.json。
4. 运行 python scripts/build_research_report.py --audit-db data/local/myinveststock.sqlite temp/assembly_inputs/{code}_stock_research_{basis_date}.json > temp/reports/{code}_stock_research_{basis_date}.json。
5. 用 python scripts/import_research_run.py temp/reports/{code}_stock_research_{basis_date}.json 入库。
5. 导入成功后，汇报 run_id、report_hash、audit_log stage 覆盖、verify_run 结果和系统生成的主要结论摘要。

{STOCK_RESEARCH_INPUT_INSTRUCTION}

完成后保证 /stocks/{code} 能看到由 deterministic pipeline 生成并入库的估值区间历史叠加。"""


def build_requested_stock_research_prompt(
    item: dict[str, Any],
    report: dict[str, Any],
    *,
    trigger_reason: str,
) -> str:
    code = item["code"]
    name = item["name"]
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股完整深研。

唯一研究对象：{code} {name}。
本次触发原因：{trigger_reason}。

入口信息：
- 入口来源：用户主动请求 /research?stock={code}
- 主线环境入口：https://theme.okbbc.com/api/index
- report_id：{report["report_id"]}
- basis_date：{report.get("basis_date")}
- 主题：其他请求

硬约束：
- 这只股票不要求出现在 /api/index 的 A可跟踪龙头列表里。
- 只研究这一只股票，禁止同时研究其他股票。
- Tushare 是 A 股结构化主源，网络资料只作补充证据。
- 如果本地已有 MyInvestLeader 个股信号，只引用其龙头证据；主线环境优先引用 MyInvestTheme /api/index，无法明确匹配主题时记录为数据缺口，不要强行绑定。
- 每次触发都输出完整个股深研，不再拆分任务类型。
- 合理估值区间必须来自确定性估值流水线。
- 不输出交易指令、不输出现金金额、不输出股数。

必须覆盖：
- 行业位置：公司所处细分赛道、产业链环节、国内/全球地位。
- 市场空间：当前空间、未来 3-5 年扩容逻辑、政策或技术驱动。
- 竞争格局：直接竞争者、替代者、潜在进入者、行业集中度变化。
- 上下游公司：关键供应商、客户、平台、渠道和议价关系。
- 战略壁垒：技术、品牌、渠道、成本、客户锁定、牌照或生态。
- 数倍潜力：只判断成立条件、约束条件和证伪条件，不用股价目标代替逻辑。
- 财务质量：增长、盈利能力、现金流、资产负债、同业对比。
- 合理估值区间：由 deterministic pipeline 生成。
- 战略证伪条件：什么行业或竞争数据出现后说明长期逻辑错了。

执行流程：
1. 构建 stock_research assembly_input，trigger_reason 固定为“{trigger_reason}”。
2. 运行 scripts/build_research_report.py 生成最终 StockResearchReport。
3. 用 scripts/import_research_run.py 入库。

{STOCK_REPORT_SCHEMA_INSTRUCTION}
JSON 必须符合 StockResearchReport：`task_type` 为 `stock_research`，必须包含完整估值区间，禁止额外字段。"""


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
    db_target = Path(db_path) if db_path is not None else DB_PATH
    init_db(db_target)
    stock_name = resolve_requested_stock_name(stock_code, requested_name=name, db_path=db_target)
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
        upsert_queue_item(
            conn,
            report_id=report["report_id"],
            code=stock_code,
            name=stock_name,
            priority=900,
            stage=1,
            task_type=TASK_TYPE_STOCK_RESEARCH,
            task_keyword=f"MyInvestStock 个股深研 {stock_code} {stock_name}",
            prompt=build_requested_stock_research_prompt(
                item,
                report,
                trigger_reason=TRIGGER_MANUAL_REQUEST,
            ),
            depends_on_task_type=None,
            trigger_reason=TRIGGER_MANUAL_REQUEST,
            task_date=basis_date,
            now=now,
            source_type=QUEUE_SOURCE_REQUEST,
            source_detail="/research",
        )
        queued.append(TASK_TYPE_STOCK_RESEARCH)
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
    theme_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db_target = Path(db_path) if db_path is not None else DB_PATH
    init_db(db_target)
    report = report_meta(payload)
    if theme_payload is not None:
        theme_meta = theme_report_meta(theme_payload)
        report["theme_report_id"] = report.get("theme_report_id") or theme_meta.get("report_id")
    items = [
        enrich_leader_item(item, theme_payload) if theme_payload is not None else item
        for item in primary_items(payload)
    ]
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
            if not has_stock_research_work(conn, item["code"]):
                upsert_queue_item(
                    conn,
                    report_id=report["report_id"],
                    code=item["code"],
                    name=item["name"],
                    priority=priority,
                    stage=1,
                    task_type=TASK_TYPE_STOCK_RESEARCH,
                    task_keyword=f"MyInvestStock 个股深研 {item['code']} {item['name']}",
                    prompt=build_stock_research_prompt(
                        item,
                        report,
                        trigger_reason=TRIGGER_TRACKABLE_LEADER,
                    ),
                    depends_on_task_type=None,
                    trigger_reason=TRIGGER_TRACKABLE_LEADER,
                    task_date=report.get("basis_date"),
                    now=now,
                )
        conn.commit()
    return {
        "report_id": report["report_id"],
        "theme_report_id": report.get("theme_report_id"),
        "basis_date": report.get("basis_date"),
        "count": len(items),
        "codes": [item["code"] for item in items],
        "names": [item["name"] for item in items],
    }
