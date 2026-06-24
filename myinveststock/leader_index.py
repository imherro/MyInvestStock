from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from .config import DB_PATH, LEADER_INDEX_URL, RAW_DATA_DIR
from .db import (
    connect,
    has_strategic_work,
    init_db,
    upsert_queue_item,
    upsert_report,
    upsert_trackable_leader,
    utc_now,
)

STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


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

完成后将 task_type='strategic' 的结构化结果写入 stock_research_runs。"""


def build_financial_prompt(item: dict[str, Any], report: dict[str, Any]) -> str:
    code = item["code"]
    name = item["name"]
    theme = item.get("theme") or ""
    report_id = report["report_id"]
    basis_date = report.get("basis_date") or ""
    return f"""在 C:\\Users\\kunpeng\\Documents\\MyInvestStock 中执行个股财务估值深研。

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
- 本任务可以多次重复执行，用最新财务、估值和价格数据刷新结论。
- 本任务专注财务质量、增长质量、估值区间和重仓研究资格，不重复写泛行业故事。
- 不输出交易指令、不输出现金金额、不输出股数。

必须覆盖：
- 财务质量：收入、利润、毛利率、净利率、ROE、现金流、负债质量。
- 年增长率：近年收入/利润增速、未来增长假设、增长可信度。
- 估值方法：按行业属性选择 PE、PEG、PB、PS、DCF 或分部估值。
- 合理估值区间：保守、合理、乐观三档，必须说明关键假设和触发条件。
- 当前价格位置：只判断高估、合理、低估或观察，不给买卖指令。
- 五倍/十倍潜力校验：用财务和估值条件验证战略深研中的潜力判断。
- 重仓资格：只能写研究标签，如 不具备、观察、可跟踪、核心仓研究资格、高估暂缓。
- 财务证伪条件：什么财务或估值数据出现后说明判断错了。

完成后将 task_type='financial' 的结构化结果写入 stock_research_runs，并保证 /stocks/{code} 能看到估值区间历史叠加。"""


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
    with connect(db_target) as conn:
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
