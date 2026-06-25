from __future__ import annotations

import html
import json
import mimetypes
import re
from contextlib import closing
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import (
    DB_PATH,
    DEFAULT_HOST,
    DEFAULT_PORT,
    FOOTER_SCRIPT_URL,
    HEADER_SCRIPT_URL,
    LEADER_INDEX_URL,
    ROOT,
    STATIC_ASSET_VERSION,
    THEME_INDEX_URL,
)
from .db import (
    connect,
    get_known_leader,
    get_latest_leader,
    latest_report,
    list_daily_prices,
    list_latest_leaders,
    list_queue,
    list_queue_for_stock,
    list_research_runs,
    list_trackable_history,
    queue_source_label,
    rows_to_dicts,
    valuation_runs,
)
from .leader_index import enqueue_requested_stock

STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
BULL_MARKET_START_DATE = "2024-09-24"


def esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def load_json(value: str | None, fallback: object) -> object:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def leader_to_summary(row: object) -> dict[str, object]:
    market = load_json(row["market_json"], {})
    scores = load_json(row["scores_json"], {})
    raw = load_json(_row_value(row, "raw_json"), {})
    theme_context = raw.get("theme_context") if isinstance(raw, dict) else None
    upstream_signal = upstream_signal_summary(row)
    return {
        "code": row["code"],
        "name": row["name"],
        "theme": row["theme"],
        "themes": load_json(row["themes_json"], []),
        "deep_rating": row["deep_rating"],
        "deep_label": row["deep_label"],
        "deep_score": row["deep_score"],
        "shadow_observation_eligible": bool(row["shadow_observation_eligible"]),
        "candidate": {
            "leader_tier": row["candidate_leader_tier"],
            "leader_claim": row["candidate_leader_claim"],
            "evidence_score": row["candidate_evidence_score"],
            "evidence_count": row["candidate_evidence_count"],
            "hard_evidence_count": row["candidate_hard_evidence_count"],
        },
        "market": market,
        "scores": scores,
        "theme_context": theme_context if isinstance(theme_context, dict) else None,
        "upstream_signal": upstream_signal,
        "risk_flags": load_json(row["risk_flags_json"], []),
        "data_gaps": load_json(row["data_gaps_json"], []),
        "links": {
            "page": f"/stocks/{row['code']}",
            "research_gateway": f"/research?stock={row['code']}",
            "api": f"/api/stocks/{row['code']}",
            "xueqiu": row["xueqiu_url"],
        },
    }


def research_run_to_summary(row: object) -> dict[str, object]:
    financial_signal = financial_signal_summary(row)
    return {
        "id": row["id"],
        "task_type": row["task_type"],
        "trigger_reason": _row_value(row, "trigger_reason"),
        "research_date": row["research_date"],
        "status": row["status"],
        "title": row["title"],
        "summary": row["summary"],
        "valuation": {
            "low": row["valuation_low"],
            "mid": row["valuation_mid"],
            "high": row["valuation_high"],
            "unit": row["valuation_unit"],
            "method": row["valuation_method"],
            "confidence": row["valuation_confidence"],
        },
        "industry_position": row["industry_position"],
        "competition_landscape": row["competition_landscape"],
        "upstream_downstream": row["upstream_downstream"],
        "annual_growth": row["annual_growth"],
        "multi_bagger_potential": row["multi_bagger_potential"],
        "heavy_position_view": row["heavy_position_view"],
        "financial_signal": financial_signal,
        "evidence": load_json(row["evidence_json"], []),
        "assumptions": load_json(row["assumptions_json"], []),
        "risks": load_json(row["risks_json"], []),
    }


def latest_stock_research(runs: list[object]) -> dict[str, object] | None:
    for row in runs:
        if row["task_type"] == "stock_research":
            return research_run_to_summary(row)
    return None


def valuation_history_payload(runs: list[object]) -> list[dict[str, object]]:
    history = []
    for row in runs:
        history.append(
            {
                "research_date": row["research_date"],
                "low": row["valuation_low"],
                "mid": row["valuation_mid"],
                "high": row["valuation_high"],
                "unit": row["valuation_unit"],
                "method": row["valuation_method"],
                "confidence": row["valuation_confidence"],
                "heavy_position_view": row["heavy_position_view"],
            }
        )
    return history


def render_layout(title: str, body: str) -> bytes:
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} | MyInvestStock</title>
  <link rel="stylesheet" href="/static/styles.css?v={STATIC_ASSET_VERSION}">
</head>
<body>
  <div data-myinvest-header></div>
  <main>
{body}
  </main>
  <div data-myinvest-footer></div>
  <script src="{HEADER_SCRIPT_URL}" data-target="[data-myinvest-header]" defer></script>
  <script src="{FOOTER_SCRIPT_URL}" data-target="[data-myinvest-footer]" defer></script>
</body>
</html>
"""
    return html_text.encode("utf-8")


def fmt_num(value: object, digits: int = 2) -> str:
    if value is None:
        return "待入库"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return esc(value)


def _num(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal_bucket(score: object, *, strong: float, weak: float) -> str:
    number = _num(score)
    if number is None:
        return "unknown"
    if number >= strong:
        return "strong"
    if number >= weak:
        return "watch"
    return "weak"


def _bucket_label(bucket: str, *, kind: str) -> str:
    if kind == "upstream":
        return {
            "strong": "上游主线信号强",
            "watch": "上游主线可跟踪",
            "weak": "上游主线偏弱",
            "unknown": "等待上游信号",
        }.get(bucket, "等待上游信号")
    return {
        "high": "财务安全边际较高",
        "medium": "财务安全边际中性",
        "low": "财务安全边际不足",
        "unknown": "等待财务估值",
    }.get(bucket, "等待财务估值")


def upstream_signal_summary(row: object | None) -> dict[str, object]:
    if row is None:
        return {
            "source": "MyInvestLeader /api/index + MyInvestTheme /api/index",
            "theme": None,
            "bucket": "unknown",
            "label": _bucket_label("unknown", kind="upstream"),
            "explanation": "未找到 MyInvestLeader 个股入口或 MyInvestTheme 主线环境快照。",
        }
    scores_value = load_json(row["scores_json"], {})
    market_value = load_json(row["market_json"], {})
    risk_flags_value = load_json(row["risk_flags_json"], [])
    themes_value = load_json(row["themes_json"], [])
    raw_value = load_json(_row_value(row, "raw_json"), {})
    theme_context = raw_value.get("theme_context") if isinstance(raw_value, dict) else None
    if not isinstance(theme_context, dict):
        theme_context = {}
    scores = scores_value if isinstance(scores_value, dict) else {}
    market = market_value if isinstance(market_value, dict) else {}
    risk_flags = risk_flags_value if isinstance(risk_flags_value, list) else []
    themes = themes_value if isinstance(themes_value, list) else []
    theme_binding = _num(scores.get("theme_binding"))
    leader_score = _num(row["deep_score"])
    evidence_quality = _num(scores.get("evidence_quality") or row["candidate_evidence_score"])
    trading_structure = _num(scores.get("trading_structure"))

    theme_bucket = str(theme_context.get("bucket") or "")
    if theme_bucket in {"strong", "watch", "weak"}:
        bucket = theme_bucket
    else:
        anchor_score = theme_binding if theme_binding is not None else leader_score
        bucket = _signal_bucket(anchor_score, strong=80.0, weak=60.0)
        if bucket == "strong" and leader_score is not None and leader_score < 65.0:
            bucket = "watch"
    label = _bucket_label(bucket, kind="upstream")
    parts = [
        f"主线强度 {fmt_num(theme_context.get('mainline_score_v6'))}",
        f"生命周期 {theme_context.get('lifecycle_state_label') or '待入库'}",
        f"周期阶段 {theme_context.get('cycle_stage_label') or '待入库'}",
        f"市场确认 {fmt_num(theme_context.get('cycle_market_score') or theme_context.get('market_score'))}",
        f"ETF/板块 {fmt_num(theme_context.get('etf_score'))}",
        f"拥挤度 {theme_context.get('crowding_signal') or '待入库'}",
        f"风险偏好 {theme_context.get('risk_appetite') or '待入库'}",
        f"主题绑定 {fmt_num(theme_binding)}",
        f"龙头深研 {fmt_num(leader_score)}",
        f"证据质量 {fmt_num(evidence_quality)}",
        f"交易结构 {fmt_num(trading_structure)}",
    ]
    return {
        "source": (
            "MyInvestLeader /api/index + MyInvestTheme /api/index"
            if theme_context
            else "MyInvestLeader /api/index"
        ),
        "theme": row["theme"],
        "themes": themes,
        "bucket": bucket,
        "label": label,
        "theme_context": theme_context or None,
        "mainline_score_v6": theme_context.get("mainline_score_v6"),
        "lifecycle_state": theme_context.get("lifecycle_state"),
        "lifecycle_state_label": theme_context.get("lifecycle_state_label"),
        "cycle_stage": theme_context.get("cycle_stage"),
        "cycle_stage_label": theme_context.get("cycle_stage_label"),
        "cycle_stage_advice": theme_context.get("cycle_stage_advice"),
        "cycle_market_score": theme_context.get("cycle_market_score"),
        "cycle_evidence_score": theme_context.get("cycle_evidence_score"),
        "market_score": theme_context.get("market_score"),
        "policy_score": theme_context.get("policy_score"),
        "etf_score": theme_context.get("etf_score"),
        "crowding_signal": theme_context.get("crowding_signal"),
        "market_state": theme_context.get("market_state"),
        "risk_appetite": theme_context.get("risk_appetite"),
        "theme_quality": {
            "data_quality_status": theme_context.get("data_quality_status"),
            "contract_validation_status": theme_context.get("contract_validation_status"),
            "policy_provenance_status": theme_context.get("policy_provenance_status"),
            "snapshot_status": theme_context.get("snapshot_status"),
            "report_id": theme_context.get("report_id"),
            "basis_date": theme_context.get("basis_date"),
        }
        if theme_context
        else None,
        "theme_binding": theme_binding,
        "leader_score": leader_score,
        "evidence_quality": evidence_quality,
        "trading_structure": trading_structure,
        "rating": f"{row['deep_rating'] or ''} {row['deep_label'] or ''}".strip(),
        "leader_claim": row["candidate_leader_claim"],
        "market": {
            "r5": market.get("r5"),
            "r20": market.get("r20"),
            "r60": market.get("r60"),
            "turnover_rate": market.get("turnover_rate"),
        },
        "risk_flags": risk_flags,
        "explanation": "；".join(parts),
    }


def financial_signal_summary(row: object | None) -> dict[str, object]:
    if row is None:
        return {
            "source": "MyInvestStock deterministic valuation",
            "bucket": "unknown",
            "label": _bucket_label("unknown", kind="financial"),
            "explanation": "等待个股深研入库。",
        }
    raw = load_json(_row_value(row, "raw_json"), {})
    valuation = raw.get("valuation") if isinstance(raw, dict) else {}
    conclusion = raw.get("conclusion") if isinstance(raw, dict) else {}
    undervalued_score = _num(valuation.get("undervalued_score")) if isinstance(valuation, dict) else None
    risk_adjusted_score = _num(valuation.get("risk_adjusted_score")) if isinstance(valuation, dict) else None
    growth_score = _num(valuation.get("growth_score")) if isinstance(valuation, dict) else None
    quality_score = _num(valuation.get("quality_score")) if isinstance(valuation, dict) else None
    if undervalued_score is None:
        bucket = "unknown"
    elif undervalued_score >= 70.0:
        bucket = "high"
    elif undervalued_score >= 40.0:
        bucket = "medium"
    else:
        bucket = "low"
    label = _bucket_label(bucket, kind="financial")
    return {
        "source": "MyInvestStock deterministic valuation",
        "bucket": bucket,
        "label": label,
        "undervalued_score": undervalued_score,
        "growth_score": growth_score,
        "quality_score": quality_score,
        "risk_adjusted_score": risk_adjusted_score,
        "valuation_range": {
            "low": _row_value(row, "valuation_low"),
            "mid": _row_value(row, "valuation_mid"),
            "high": _row_value(row, "valuation_high"),
            "unit": _row_value(row, "valuation_unit"),
            "method": _row_value(row, "valuation_method"),
        },
        "raw_grade": _row_value(row, "heavy_position_view"),
        "raw_summary": conclusion.get("summary") if isinstance(conclusion, dict) else None,
        "explanation": (
            f"财务安全 {fmt_num(undervalued_score)}；增长 {fmt_num(growth_score)}；"
            f"质量 {fmt_num(quality_score)}；风险调整 {fmt_num(risk_adjusted_score)}"
        ),
    }


def decision_matrix_summary(
    upstream_signal: dict[str, object],
    financial_signal: dict[str, object],
) -> dict[str, object]:
    upstream_bucket = str(upstream_signal.get("bucket") or "unknown")
    financial_bucket = str(financial_signal.get("bucket") or "unknown")
    if upstream_bucket == "unknown":
        conclusion = "等待上游主线信号"
        posture = "待确认"
    elif financial_bucket == "unknown":
        conclusion = "主线信号已入库，等待财务安全边际验证"
        posture = "待财务深研"
    elif upstream_bucket == "strong" and financial_bucket == "high":
        conclusion = "上游主线强，财务安全边际较高，进入核心候选研究"
        posture = "核心候选研究"
    elif upstream_bucket == "strong" and financial_bucket in {"medium", "low"}:
        conclusion = "上游主线强，但财务安全边际不足，作为主线弹性跟踪对象，不按安全边际重仓"
        posture = "主线弹性跟踪"
    elif upstream_bucket in {"watch", "weak"} and financial_bucket == "high":
        conclusion = "财务安全边际较高，但上游主线信号未确认，适合作为价值观察对象等待催化"
        posture = "价值观察"
    elif upstream_bucket == "watch" and financial_bucket == "medium":
        conclusion = "主线和财务都处于中性区间，继续观察趋势、估值和业绩兑现"
        posture = "观察"
    else:
        conclusion = "上游主线信号偏弱且财务安全边际不足，优先等待风险释放"
        posture = "风险释放优先"
    return {
        "upstream_bucket": upstream_bucket,
        "financial_bucket": financial_bucket,
        "upstream_label": upstream_signal.get("label"),
        "financial_label": financial_signal.get("label"),
        "posture": posture,
        "conclusion": conclusion,
        "rule": "MyInvestTheme mainline environment + MyInvestLeader stock signal + MyInvestStock financial safety margin matrix",
    }


def score_state(value: object, *, kind: str = "default") -> str:
    number = _num(value)
    if number is None:
        return "待入库"
    if kind == "valuation_safety":
        if number >= 85:
            return "估值安全边际较高"
        if number >= 70:
            return "估值相对可接受"
        if number >= 50:
            return "估值中性，需结合增长验证"
        return "估值压力较高"
    if kind == "evidence_quality":
        if number >= 85:
            return "证据强，龙头判断较扎实"
        if number >= 70:
            return "证据较充分"
        if number >= 60:
            return "证据可观察，仍需深研确认"
        return "证据偏弱"
    if kind == "deep_score":
        if number >= 80:
            return "高优先级深研对象"
        if number >= 70:
            return "可跟踪深研对象"
        if number >= 60:
            return "观察型候选"
        return "低优先级候选"
    if number >= 85:
        return "强"
    if number >= 70:
        return "较好"
    if number >= 60:
        return "中性"
    return "偏弱"


def score_signal(value: object, *, kind: str = "default") -> tuple[str, str]:
    number = _num(value)
    if number is None:
        return "unknown", "待入库"
    if kind == "valuation_safety":
        if number >= 85:
            return "safe", "低估/安全"
        if number >= 70:
            return "ok", "估值可接受"
        if number >= 50:
            return "watch", "估值中性"
        return "danger", "估值危险"
    if kind == "evidence_quality":
        if number >= 85:
            return "safe", "证据可信"
        if number >= 70:
            return "ok", "证据较足"
        if number >= 60:
            return "watch", "需确认"
        return "danger", "证据偏弱"
    if kind == "deep_score":
        if number >= 80:
            return "safe", "高优先级"
        if number >= 70:
            return "ok", "可跟踪"
        if number >= 60:
            return "watch", "观察"
        return "danger", "低优先"
    if number >= 85:
        return "safe", "强"
    if number >= 70:
        return "ok", "较好"
    if number >= 60:
        return "watch", "中性"
    return "danger", "偏弱"


def ratio_state(label: str, value: object) -> str:
    number = _num(value)
    if number is None:
        return "待入库"
    if label == "PE TTM":
        if number <= 0:
            return "亏损或口径不适用"
        if number < 15:
            return "低市盈率区间"
        if number < 30:
            return "中等市盈率区间"
        if number < 60:
            return "较高市盈率，需增长兑现"
        return "高市盈率，需强增长支撑"
    if label == "PB":
        if number < 1:
            return "低于净资产定价"
        if number < 3:
            return "常见市净率区间"
        if number < 6:
            return "较高市净率，需高 ROE 支撑"
        return "高市净率，需强盈利质量支撑"
    return "行情快照"


def ratio_signal(label: str, value: object) -> tuple[str, str]:
    number = _num(value)
    if number is None:
        return "unknown", "待入库"
    if label == "PE TTM":
        if number <= 0:
            return "danger", "亏损/异常"
        if number < 15:
            return "safe", "低估"
        if number < 30:
            return "ok", "合理"
        if number < 60:
            return "watch", "偏贵"
        return "danger", "危险"
    if label == "PB":
        if number < 1:
            return "safe", "资产折价"
        if number < 3:
            return "ok", "合理"
        if number < 6:
            return "watch", "偏贵"
        return "danger", "危险"
    return "neutral", "行情快照"


def metric_explanation(label: str, value: object) -> tuple[str, str]:
    if label in {"深研", "深研分"}:
        return (
            "综合入口评分",
            f"{score_state(value, kind='deep_score')}。衡量这只股票是否值得进入个股深研队列，不等于最终重仓结论。",
        )
    if label == "收盘":
        return (
            "行情快照",
            "基准数据中的收盘价，单位通常为元/股；它是价格参照，不代表合理估值。",
        )
    if label in {"PE TTM", "PB"}:
        return (
            "估值倍数",
            f"{ratio_state(label, value)}。这是入口快照口径，仍需结合行业、增长、ROE 和现金流判断。",
        )
    if label == "证据质量":
        return (
            "龙头证据强度",
            f"{score_state(value, kind='evidence_quality')}。分数越高，说明支持龙头地位的硬证据越充分。",
        )
    if label == "估值安全":
        return (
            "入口估值安全度",
            f"{score_state(value, kind='valuation_safety')}。分数越高，表示入口筛选看估值越不紧张；最终估值区间以后续财务深研为准。",
        )
    return ("指标说明", "入口展示指标，用于辅助筛选和跟踪。")


def metric_signal(label: str, value: object) -> tuple[str, str]:
    if label in {"深研", "深研分"}:
        return score_signal(value, kind="deep_score")
    if label == "收盘":
        return "neutral", "行情快照"
    if label in {"PE TTM", "PB"}:
        return ratio_signal(label, value)
    if label == "证据质量":
        return score_signal(value, kind="evidence_quality")
    if label == "估值安全":
        return score_signal(value, kind="valuation_safety")
    return "neutral", "参考"


def metric(label: str, value: object, unit: str = "") -> str:
    shown = fmt_num(value) if isinstance(value, (int, float)) else esc(value or "待入库")
    tooltip_title, tooltip_body = metric_explanation(label, value)
    signal_class, signal_label = metric_signal(label, value)
    tooltip_text = f"{tooltip_title}：{tooltip_body}"
    return f"""<div class="metric metric-signal-{esc(signal_class)}" tabindex="0" title="{esc(tooltip_text)}" aria-label="{esc(label)}：{esc(shown)}{esc(unit)}。{esc(signal_label)}。{esc(tooltip_text)}">
      <span>{esc(label)}</span>
      <strong>{shown}{esc(unit)}</strong>
      <small class="metric-signal-label">{esc(signal_label)}</small>
      <div class="metric-tooltip" role="tooltip">
        <b>{esc(tooltip_title)}</b>
        <em>{esc(tooltip_body)}</em>
      </div>
    </div>"""


def xueqiu_url_for_code(code: object, preferred_url: object | None = None) -> str:
    if preferred_url:
        return str(preferred_url)
    text = str(code)
    if "." not in text:
        return "https://xueqiu.com/"
    symbol, exchange = text.split(".", 1)
    return f"https://xueqiu.com/S/{exchange.upper()}{symbol}"


def stock_page_link(code: object, label: object) -> str:
    safe_code = esc(code)
    return f"""<a class="table-link" href="/stocks/{safe_code}">{esc(label)}</a>"""


def xueqiu_stock_link(code: object, preferred_url: object | None = None) -> str:
    return (
        f"""<a class="code-link" href="{esc(xueqiu_url_for_code(code, preferred_url))}" """
        f"""target="_blank" rel="noopener noreferrer">{esc(code)}</a>"""
    )


def render_queue_rows(queue: list[object]) -> str:
    if not queue:
        return '<tr><td colspan="9" class="empty-cell">当前队列为空。</td></tr>'
    return "".join(
        f"""<tr>
      <td>{esc(row['priority'])}</td>
      <td>{esc(row['stage'])}</td>
      <td>{esc(queue_source_label(row['source_type']))}</td>
      <td>{esc(row['trigger_reason'] or '待记录')}</td>
      <td>{xueqiu_stock_link(row['code'])}</td>
      <td>{stock_page_link(row['code'], row['name'])}</td>
      <td>{esc(row['task_type'])}</td>
      <td>{esc(row['status'])}</td>
      <td>{esc(row['task_keyword'])}</td>
    </tr>"""
        for row in queue
    )


def render_home() -> bytes:
    with closing(connect(DB_PATH)) as conn:
        report = latest_report(conn)
        leaders = list_latest_leaders(conn)
        queue = list_queue(conn)
    if not report:
        body = """
    <section class="page-band">
      <div class="content">
        <h1>A可跟踪龙头</h1>
        <p class="muted">本地还没有入库数据。先运行 <code>python scripts/ingest_index.py</code>。</p>
      </div>
    </section>
"""
        return render_layout("A可跟踪龙头", body)

    cards = []
    for row in leaders:
        market = load_json(row["market_json"], {})
        scores = load_json(row["scores_json"], {})
        cards.append(
            f"""<article class="stock-card">
        <div>
          <a class="stock-title" href="/stocks/{esc(row['code'])}">{esc(row['name'])}</a>
          <div class="stock-code">{xueqiu_stock_link(row['code'], row['xueqiu_url'])}</div>
        </div>
        <div class="badges">
          <span class="badge badge-strong">{esc(row['deep_rating'] or '')} {esc(row['deep_label'] or '')}</span>
          <span class="badge">{esc(row['theme'] or '')}</span>
        </div>
        <div class="card-grid">
          {metric("深研", row["deep_score"])}
          {metric("收盘", market.get("close"))}
          {metric("PE TTM", market.get("pe_ttm"))}
          {metric("估值安全", scores.get("valuation_safety"))}
        </div>
        <a class="text-link" href="/stocks/{esc(row['code'])}">查看个股页</a>
      </article>"""
        )

    queue_rows = render_queue_rows(queue)
    body = f"""
    <section class="page-band">
      <div class="content">
        <div class="page-title-row">
          <div>
            <h1>A可跟踪龙头</h1>
            <p class="muted">入口固定为 <code>/api/index</code> 的 <code>key_results.primary_output.items</code>。</p>
          </div>
          <div class="report-box">
            <span>report_id</span>
            <strong>{esc(report['report_id'])}</strong>
            <span>basis_date {esc(report['basis_date'])}</span>
          </div>
        </div>
      </div>
    </section>
    <section class="content stock-grid">
      {''.join(cards)}
    </section>
    <section class="content section-block">
      <h2>个股深研队列</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>优先级</th><th>阶段</th><th>来源</th><th>触发原因</th><th>代码</th><th>名称</th><th>类型</th><th>状态</th><th>任务关键词</th></tr></thead>
          <tbody>{queue_rows}</tbody>
        </table>
      </div>
    </section>
"""
    return render_layout("A可跟踪龙头", body)


def render_empty_section(title: str) -> str:
    return f"""<section class="section-block">
      <h2>{esc(title)}</h2>
      <p class="empty">等待个股深研入库。</p>
    </section>"""


def short_date(value: object) -> str:
    text = str(value or "")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text[5:]
    return text


def _chart_x(index: int, count: int, left: float, width: float) -> float:
    if count <= 1:
        return left + width / 2.0
    return left + width * index / (count - 1)


def _chart_y(value: float, lower: float, upper: float, top: float, height: float) -> float:
    if upper <= lower:
        return top + height / 2.0
    return top + (upper - value) / (upper - lower) * height


def _valuation_chart_points(runs: list[object]) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for row in runs:
        try:
            low = float(row["valuation_low"])
            mid = float(row["valuation_mid"])
            high = float(row["valuation_high"])
        except (KeyError, TypeError, ValueError):
            continue
        if high < low:
            low, high = high, low
        points.append(
            {
                "date": str(row["research_date"]),
                "low": low,
                "mid": mid,
                "high": high,
                "method": row["valuation_method"] or "待入库",
                "grade": row["heavy_position_view"] or "待入库",
            }
        )
    return points


def _row_value(row: object, key: str) -> object:
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            return row.get(key)
        return None


def _daily_price_points(prices: list[object]) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for row in prices:
        try:
            open_price = float(_row_value(row, "open_price"))
            high_price = float(_row_value(row, "high_price"))
            low_price = float(_row_value(row, "low_price"))
            close_price = float(_row_value(row, "close_price"))
        except (TypeError, ValueError):
            continue
        if high_price < low_price:
            high_price, low_price = low_price, high_price
        points.append(
            {
                "date": str(_row_value(row, "trade_date")),
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
            }
        )
    return points


def _parsed_date(value: object) -> object | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _render_plain_valuation_chart(points: list[dict[str, object]]) -> str:
    if not points:
        return render_empty_section("参考价格区间历史")

    width = 760.0
    height = 320.0
    left = 64.0
    right = 24.0
    top = 28.0
    bottom = 52.0
    plot_width = width - left - right
    plot_height = height - top - bottom
    lows = [float(item["low"]) for item in points]
    highs = [float(item["high"]) for item in points]
    lower = min(lows)
    upper = max(highs)
    span = upper - lower
    pad = max(span * 0.08, max(abs(upper), 1.0) * 0.02, 1.0)
    y_min = lower - pad
    y_max = upper + pad

    positioned = []
    count = len(points)
    for index, point in enumerate(points):
        x = _chart_x(index, count, left, plot_width)
        positioned.append(
            {
                **point,
                "x": x,
                "y_low": _chart_y(float(point["low"]), y_min, y_max, top, plot_height),
                "y_mid": _chart_y(float(point["mid"]), y_min, y_max, top, plot_height),
                "y_high": _chart_y(float(point["high"]), y_min, y_max, top, plot_height),
            }
        )

    tick_lines = []
    for index in range(5):
        value = y_max - (y_max - y_min) * index / 4.0
        y = _chart_y(value, y_min, y_max, top, plot_height)
        tick_lines.append(
            f"""<g>
          <line class="valuation-grid-line" x1="{left:.1f}" y1="{y:.1f}" x2="{width - right:.1f}" y2="{y:.1f}"></line>
          <text class="valuation-axis-label" x="{left - 10:.1f}" y="{y + 4:.1f}" text-anchor="end">{fmt_num(value)}</text>
        </g>"""
        )

    if count > 1:
        upper_points = " ".join(f"{item['x']:.1f},{item['y_high']:.1f}" for item in positioned)
        lower_points = " ".join(f"{item['x']:.1f},{item['y_low']:.1f}" for item in reversed(positioned))
        band_svg = f"""<polygon class="valuation-band" points="{upper_points} {lower_points}"></polygon>"""
        high_line = f"""<polyline class="valuation-boundary-line" points="{upper_points}"></polyline>"""
        low_line = f"""<polyline class="valuation-boundary-line" points="{" ".join(f"{item['x']:.1f},{item['y_low']:.1f}" for item in positioned)}"></polyline>"""
        mid_line = f"""<polyline class="valuation-mid-line" points="{" ".join(f"{item['x']:.1f},{item['y_mid']:.1f}" for item in positioned)}"></polyline>"""
    else:
        band_svg = ""
        high_line = ""
        low_line = ""
        mid_line = ""

    label_step = max(1, (count + 5) // 6)
    x_labels = []
    markers = []
    for index, item in enumerate(positioned):
        if index % label_step == 0 or index == count - 1:
            x_labels.append(
                f"""<text class="valuation-date-label" x="{item['x']:.1f}" y="{height - 18:.1f}" text-anchor="middle">{esc(short_date(item['date']))}</text>"""
            )
        tooltip = (
            f"{item['date']} | 保守 {fmt_num(item['low'])} | 合理 {fmt_num(item['mid'])} | "
            f"乐观 {fmt_num(item['high'])} | {item['method']} | {item['grade']}"
        )
        markers.append(
            f"""<g class="valuation-point">
          <title>{esc(tooltip)}</title>
          <line class="valuation-whisker" x1="{item['x']:.1f}" y1="{item['y_high']:.1f}" x2="{item['x']:.1f}" y2="{item['y_low']:.1f}"></line>
          <circle class="valuation-mid-dot" cx="{item['x']:.1f}" cy="{item['y_mid']:.1f}" r="4.5"></circle>
        </g>"""
        )

    return f"""<section class="section-block">
      <h2>参考价格区间历史</h2>
      <div class="valuation-chart">
        <svg class="valuation-history-svg" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="参考价格区间随时间变化图">
          <title>参考价格区间随时间变化图</title>
          <line class="valuation-axis-line" x1="{left:.1f}" y1="{top:.1f}" x2="{left:.1f}" y2="{height - bottom:.1f}"></line>
          <line class="valuation-axis-line" x1="{left:.1f}" y1="{height - bottom:.1f}" x2="{width - right:.1f}" y2="{height - bottom:.1f}"></line>
          <text class="valuation-axis-title" x="{left:.1f}" y="16" text-anchor="start">价格 CNY/share</text>
          {''.join(tick_lines)}
          {band_svg}
          {high_line}
          {low_line}
          {mid_line}
          {''.join(markers)}
          {''.join(x_labels)}
        </svg>
        <div class="valuation-legend">
          <span><i class="legend-band"></i>保守-乐观参考区间</span>
          <span><i class="legend-line"></i>合理估值中枢</span>
          <span><i class="legend-dot"></i>单次财务深研</span>
        </div>
      </div>
    </section>"""


def _price_index_on_or_after(price_dates: list[object], date_value: object) -> int:
    target = _parsed_date(date_value)
    if target is None:
        return 0
    for index, price_date in enumerate(price_dates):
        if price_date is not None and price_date >= target:
            return index
    return max(len(price_dates) - 1, 0)


def _render_close_price_valuation_chart(
    valuation_points: list[dict[str, object]],
    price_points: list[dict[str, object]],
) -> str:
    if len(price_points) < 2:
        return _render_plain_valuation_chart(valuation_points)

    width = 760.0
    height = 360.0
    left = 64.0
    right = 24.0
    top = 30.0
    bottom = 58.0
    plot_width = width - left - right
    plot_height = height - top - bottom
    plot_right = width - right
    plot_bottom = height - bottom

    price_closes = [float(item["close"]) for item in price_points]
    valuation_lows = [float(item["low"]) for item in valuation_points]
    valuation_highs = [float(item["high"]) for item in valuation_points]
    lower = min(price_closes + valuation_lows)
    upper = max(price_closes + valuation_highs)
    span = upper - lower
    pad = max(span * 0.08, max(abs(upper), 1.0) * 0.02, 1.0)
    y_min = lower - pad
    y_max = upper + pad

    price_dates = [_parsed_date(item["date"]) for item in price_points]
    price_count = len(price_points)
    spacing = plot_width / (price_count - 1)

    tick_lines = []
    for index in range(5):
        value = y_max - (y_max - y_min) * index / 4.0
        y = _chart_y(value, y_min, y_max, top, plot_height)
        tick_lines.append(
            f"""<g>
          <line class="valuation-grid-line" x1="{left:.1f}" y1="{y:.1f}" x2="{plot_right:.1f}" y2="{y:.1f}"></line>
          <text class="valuation-axis-label" x="{left - 10:.1f}" y="{y + 4:.1f}" text-anchor="end">{fmt_num(value)}</text>
        </g>"""
        )

    label_step = max(1, (price_count + 5) // 6)
    x_labels = []
    close_line_points = []
    for index, item in enumerate(price_points):
        x = _chart_x(index, price_count, left, plot_width)
        y_close = _chart_y(float(item["close"]), y_min, y_max, top, plot_height)
        close_line_points.append(f"{x:.1f},{y_close:.1f}")
        if index % label_step == 0 or index == price_count - 1:
            x_labels.append(
                f"""<text class="valuation-date-label" x="{x:.1f}" y="{height - 18:.1f}" text-anchor="middle">{esc(short_date(item['date']))}</text>"""
            )
    close_line = (
        f"""<polyline class="close-price-line" points="{' '.join(close_line_points)}">
          <title>{esc(BULL_MARKET_START_DATE)}以来收盘价折线</title>
        </polyline>"""
    )
    current_price = float(price_points[-1]["close"])
    current_date = price_points[-1]["date"]
    current_y = _chart_y(current_price, y_min, y_max, top, plot_height)
    current_label_y = min(max(current_y - 6.0, top + 12.0), plot_bottom - 6.0)
    current_line = f"""<g class="current-price-layer">
          <title>{esc(current_date)} 当前价 {fmt_num(current_price)}</title>
          <line class="current-price-line" x1="{left:.1f}" y1="{current_y:.1f}" x2="{plot_right:.1f}" y2="{current_y:.1f}"></line>
          <text class="current-price-label" x="{plot_right - 6:.1f}" y="{current_label_y:.1f}" text-anchor="end">当前价 {fmt_num(current_price)}</text>
        </g>"""

    positioned_valuations = []
    for point in valuation_points:
        price_index = _price_index_on_or_after(price_dates, point["date"])
        x = _chart_x(price_index, price_count, left, plot_width)
        positioned_valuations.append(
            {
                **point,
                "price_index": price_index,
                "x": x,
                "y_low": _chart_y(float(point["low"]), y_min, y_max, top, plot_height),
                "y_mid": _chart_y(float(point["mid"]), y_min, y_max, top, plot_height),
                "y_high": _chart_y(float(point["high"]), y_min, y_max, top, plot_height),
            }
        )

    bands = []
    boundary_lines = []
    mid_lines = []
    markers = []
    for index, item in enumerate(positioned_valuations):
        start_x = float(item["x"])
        if index + 1 < len(positioned_valuations):
            end_x = float(positioned_valuations[index + 1]["x"])
            if end_x <= start_x:
                end_x = min(plot_right, start_x + spacing)
        else:
            end_x = plot_right
        width_value = max(end_x - start_x, 2.0)
        band_y = float(item["y_high"])
        band_height = max(float(item["y_low"]) - band_y, 1.0)
        tooltip = (
            f"{item['date']} 起 | 保守 {fmt_num(item['low'])} | 合理 {fmt_num(item['mid'])} | "
            f"乐观 {fmt_num(item['high'])} | {item['method']} | {item['grade']}"
        )
        bands.append(
            f"""<rect class="valuation-step-band" x="{start_x:.1f}" y="{band_y:.1f}" width="{width_value:.1f}" height="{band_height:.1f}">
          <title>{esc(tooltip)}</title>
        </rect>"""
        )
        boundary_lines.append(
            f"""<line class="valuation-step-boundary-line" x1="{start_x:.1f}" y1="{item['y_high']:.1f}" x2="{end_x:.1f}" y2="{item['y_high']:.1f}"></line>
          <line class="valuation-step-boundary-line" x1="{start_x:.1f}" y1="{item['y_low']:.1f}" x2="{end_x:.1f}" y2="{item['y_low']:.1f}"></line>"""
        )
        mid_lines.append(
            f"""<line class="valuation-mid-line" x1="{start_x:.1f}" y1="{item['y_mid']:.1f}" x2="{end_x:.1f}" y2="{item['y_mid']:.1f}"></line>"""
        )
        markers.append(
            f"""<g class="valuation-point">
          <title>{esc(tooltip)}</title>
          <line class="valuation-whisker" x1="{start_x:.1f}" y1="{item['y_high']:.1f}" x2="{start_x:.1f}" y2="{item['y_low']:.1f}"></line>
          <circle class="valuation-mid-dot" cx="{start_x:.1f}" cy="{item['y_mid']:.1f}" r="4.5"></circle>
        </g>"""
        )

    first_date = price_points[0]["date"]
    last_date = price_points[-1]["date"]
    return f"""<section class="section-block">
      <h2>参考价格区间历史</h2>
      <div class="valuation-chart">
        <svg class="valuation-history-svg" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="收盘价折线叠加参考价格区间图">
          <title>收盘价折线叠加参考价格区间图</title>
          <line class="valuation-axis-line" x1="{left:.1f}" y1="{top:.1f}" x2="{left:.1f}" y2="{plot_bottom:.1f}"></line>
          <line class="valuation-axis-line" x1="{left:.1f}" y1="{plot_bottom:.1f}" x2="{plot_right:.1f}" y2="{plot_bottom:.1f}"></line>
          <text class="valuation-axis-title" x="{left:.1f}" y="16" text-anchor="start">价格 CNY/share</text>
          <text class="valuation-range-label" x="{plot_right:.1f}" y="16" text-anchor="end">{esc(short_date(first_date))} - {esc(short_date(last_date))}</text>
          {''.join(tick_lines)}
          <g class="close-price-layer">{close_line}</g>
          <g class="valuation-overlay-layer">
            {''.join(bands)}
            {''.join(boundary_lines)}
            {''.join(mid_lines)}
            {''.join(markers)}
          </g>
          {current_line}
          {''.join(x_labels)}
        </svg>
        <div class="valuation-legend">
          <span><i class="legend-close"></i>{esc(BULL_MARKET_START_DATE)}以来收盘价</span>
          <span><i class="legend-current"></i>当前价格</span>
          <span><i class="legend-band"></i>保守-乐观参考区间</span>
          <span><i class="legend-line"></i>合理估值中枢</span>
          <span><i class="legend-dot"></i>个股深研刷新点</span>
        </div>
      </div>
    </section>"""


def render_valuation_chart(runs: list[object], prices: list[object] | None = None) -> str:
    points = _valuation_chart_points(runs)
    if not points:
        return render_empty_section("参考价格区间历史")
    price_points = _daily_price_points(prices or [])
    if price_points:
        return _render_close_price_valuation_chart(points, price_points)
    return _render_plain_valuation_chart(points)


def render_stock_queue_status(queue: list[object]) -> str:
    if not queue:
        return ""
    rows = "".join(
        f"""<tr>
      <td>{esc(row['task_type'])}</td>
      <td>{esc(queue_source_label(row['source_type']))}</td>
      <td>{esc(row['trigger_reason'] or '待记录')}</td>
      <td>{esc(row['status'])}</td>
      <td>{esc(row['task_keyword'])}</td>
      <td>{esc(row['updated_at'])}</td>
    </tr>"""
        for row in queue
    )
    return f"""<section class="section-block">
        <h2>研究队列状态</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>类型</th><th>来源</th><th>触发原因</th><th>状态</th><th>任务关键词</th><th>更新时间</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
        </div>
      </section>"""


def signal_item(label: str, value: object, detail: object | None = None) -> str:
    detail_html = f"<small>{esc(detail)}</small>" if detail is not None else ""
    return f"""<div class="signal-item">
      <span>{esc(label)}</span>
      <strong>{esc(value if value is not None else '待入库')}</strong>
      {detail_html}
    </div>"""


def render_signal_matrix(
    upstream_signal: dict[str, object],
    financial_signal: dict[str, object],
    matrix: dict[str, object],
) -> str:
    upstream_risk_flags = upstream_signal.get("risk_flags")
    if not isinstance(upstream_risk_flags, list):
        upstream_risk_flags = []
    risk_text = "；".join(str(item) for item in upstream_risk_flags) or "暂无上游风险提示"
    valuation_range = financial_signal.get("valuation_range")
    range_text = "等待财务估值"
    if isinstance(valuation_range, dict) and valuation_range.get("mid") is not None:
        range_text = (
            f"{fmt_num(valuation_range.get('low'))} / {fmt_num(valuation_range.get('mid'))} / "
            f"{fmt_num(valuation_range.get('high'))}"
        )
    return f"""<section class="section-block">
        <h2>主线信号与财务安全边际</h2>
        <div class="signal-matrix">
          <div class="signal-panel signal-panel-upstream">
            <h3>上游主线信号</h3>
            <p class="muted">主线环境来自 MyInvestTheme，个股龙头入口来自 MyInvestLeader，本项目不重复研究主线。</p>
            <div class="signal-grid">
              {signal_item("所属主题", upstream_signal.get("theme"))}
              {signal_item("主线状态", upstream_signal.get("label"), upstream_signal.get("rating"))}
              {signal_item("生命周期", upstream_signal.get("lifecycle_state_label"))}
              {signal_item("周期阶段", upstream_signal.get("cycle_stage_label"), upstream_signal.get("cycle_stage_advice"))}
              {signal_item("主线强度", fmt_num(upstream_signal.get("mainline_score_v6")))}
              {signal_item("市场确认", fmt_num(upstream_signal.get("cycle_market_score") or upstream_signal.get("market_score")))}
              {signal_item("ETF/板块", fmt_num(upstream_signal.get("etf_score")))}
              {signal_item("拥挤度", upstream_signal.get("crowding_signal"))}
              {signal_item("风险偏好", upstream_signal.get("risk_appetite"), upstream_signal.get("market_state"))}
              {signal_item("主题绑定", fmt_num(upstream_signal.get("theme_binding")))}
              {signal_item("龙头深研", fmt_num(upstream_signal.get("leader_score")))}
              {signal_item("证据质量", fmt_num(upstream_signal.get("evidence_quality")))}
              {signal_item("交易结构", fmt_num(upstream_signal.get("trading_structure")))}
            </div>
            <p class="signal-note">龙头证据：{esc(upstream_signal.get("leader_claim") or "待入库")}</p>
            <p class="signal-note">上游风险：{esc(risk_text)}</p>
          </div>
          <div class="signal-panel signal-panel-financial">
            <h3>财务安全边际</h3>
            <p class="muted">来自 MyInvestStock 确定性估值，只判断财务能否支撑安全边际。</p>
            <div class="signal-grid">
              {signal_item("安全边际", financial_signal.get("label"))}
              {signal_item("估值区间", range_text, financial_signal.get("source"))}
              {signal_item("财务安全", fmt_num(financial_signal.get("undervalued_score")))}
              {signal_item("增长", fmt_num(financial_signal.get("growth_score")))}
              {signal_item("质量", fmt_num(financial_signal.get("quality_score")))}
              {signal_item("风险调整", fmt_num(financial_signal.get("risk_adjusted_score")))}
            </div>
            <p class="signal-note">估值模型原始标签：{esc(financial_signal.get("raw_grade") or "待入库")}</p>
          </div>
          <div class="matrix-conclusion">
            <span>矩阵结论</span>
            <strong>{esc(matrix.get("posture"))}</strong>
            <p>{esc(matrix.get("conclusion"))}</p>
          </div>
        </div>
      </section>"""


def render_trackable_history(rows: list[object]) -> str:
    if not rows:
        return """<section class="section-block">
        <h2>可跟踪龙头历史</h2>
        <p class="empty">尚未在本地记录中被列为 A可跟踪龙头。</p>
      </section>"""
    body = "".join(
        f"""<tr>
      <td>{esc(row['basis_date'] or short_date(row['generated_at'] or row['fetched_at']))}</td>
      <td>{esc(row['deep_rating'] or '')} {esc(row['deep_label'] or '')}</td>
      <td>{fmt_num(row['deep_score'])}</td>
      <td>{esc(row['theme'] or '待入库')}</td>
      <td>{esc(row['candidate_leader_claim'] or '待入库')}</td>
      <td>{esc(row['report_id'])}</td>
    </tr>"""
        for row in rows
    )
    return f"""<section class="section-block">
        <h2>可跟踪龙头历史</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>日期</th><th>评级</th><th>深研分</th><th>主题</th><th>龙头证据</th><th>report_id</th></tr></thead>
            <tbody>{body}</tbody>
          </table>
        </div>
      </section>"""


def _first_queue_name(queue: list[object], code: str) -> str:
    if queue:
        return str(queue[0]["name"] or code)
    return code


def _stock_exists(conn: object, code: str) -> tuple[bool, str | None]:
    leader = get_latest_leader(conn, code) or get_known_leader(conn, code)
    runs = list_research_runs(conn, code)
    if runs:
        return True, str(runs[0]["name"])
    queue = list_queue_for_stock(conn, code)
    if queue:
        return True, str(queue[0]["name"])
    if leader is not None:
        return False, str(leader["name"])
    return False, None


def normalize_stock_query(params: dict[str, list[str]]) -> tuple[str | None, str | None]:
    stock = (params.get("stock") or params.get("code") or [""])[0].strip().upper()
    name = (params.get("name") or [""])[0].strip()
    return (stock or None), (name or None)


def render_stock_page(code: str) -> bytes:
    if not STOCK_CODE_RE.match(code):
        return render_layout("无效代码", "<section class=\"content\"><h1>无效股票代码</h1></section>")
    with closing(connect(DB_PATH)) as conn:
        leader = get_latest_leader(conn, code) or get_known_leader(conn, code)
        runs = list_research_runs(conn, code)
        stock_queue = list_queue_for_stock(conn, code)
        trackable_history = list_trackable_history(conn, code)
        chart_runs = valuation_runs(conn, code)
        chart_prices = list_daily_prices(conn, code, start_date=BULL_MARKET_START_DATE)
        report = latest_report(conn)
    if leader is None and not runs and not stock_queue:
        return render_layout(
            "未找到",
            f"""<section class="content">
        <div class="section-block">
          <h1>未找到 {esc(code)}</h1>
          <p class="muted">可以通过 <a class="text-link" href="/research?stock={esc(code)}">加入个股深研队列</a> 生成研究页面。</p>
        </div>
      </section>""",
        )

    market = load_json(leader["market_json"], {}) if leader is not None else {}
    scores = load_json(leader["scores_json"], {}) if leader is not None else {}
    risk_flags = load_json(leader["risk_flags_json"], []) if leader is not None else []
    latest = next((dict(row) for row in runs if row["task_type"] == "stock_research"), {})
    risks = load_json(latest.get("risks_json"), []) if latest else []
    risk_items = "".join(f"<li>{esc(item)}</li>" for item in (risks or risk_flags or []))
    stock_name = (
        str(leader["name"])
        if leader is not None
        else str((latest or {}).get("name") or _first_queue_name(stock_queue, code))
    )
    stock_theme = leader["theme"] if leader is not None else "其他请求"
    stock_claim = leader["candidate_leader_claim"] if leader is not None else "主动研究请求"
    xueqiu_url = leader["xueqiu_url"] if leader is not None else None
    upstream_signal = upstream_signal_summary(leader)
    financial_signal = financial_signal_summary(latest if latest else None)
    decision_matrix = decision_matrix_summary(upstream_signal, financial_signal)
    rating_label = (
        f"{leader['deep_rating'] or ''} {leader['deep_label'] or ''}".strip()
        if leader is not None
        else (queue_source_label(stock_queue[0]["source_type"]) if stock_queue else "待研究")
    )
    report_date = report["basis_date"] if report else ""
    queue_status_section = render_stock_queue_status(stock_queue)
    signal_matrix_section = render_signal_matrix(upstream_signal, financial_signal, decision_matrix)
    trackable_history_section = render_trackable_history(trackable_history)

    history_rows = "".join(
        f"""<tr>
      <td>{esc(row['research_date'])}</td>
      <td>{esc(row['task_type'])}</td>
      <td>{esc(row['trigger_reason'] or '待记录')}</td>
      <td>{esc(row['status'])}</td>
      <td>{esc(row['valuation_method'] or '待入库')}</td>
      <td>{fmt_num(row['valuation_low'])} / {fmt_num(row['valuation_mid'])} / {fmt_num(row['valuation_high'])}</td>
      <td>{esc(row['heavy_position_view'] or '待入库')}</td>
    </tr>"""
        for row in runs
    )
    if not history_rows:
        history_rows = "<tr><td colspan=\"7\" class=\"empty-cell\">等待个股深研入库。</td></tr>"

    body = f"""
    <section class="page-band">
      <div class="content">
        <div class="page-title-row">
          <div>
            <h1>{esc(stock_name)}</h1>
            <p class="muted">{xueqiu_stock_link(code, xueqiu_url)} · {esc(stock_theme)} · {esc(stock_claim)}</p>
          </div>
          <div class="report-box">
            <span>研究来源</span>
            <strong>{esc(rating_label)}</strong>
            <span>{esc(report_date)}</span>
          </div>
        </div>
        <div class="summary-grid">
          {metric("深研分", leader["deep_score"] if leader is not None else None)}
          {metric("收盘", market.get("close"))}
          {metric("PE TTM", market.get("pe_ttm"))}
          {metric("PB", market.get("pb"))}
          {metric("证据质量", scores.get("evidence_quality"))}
          {metric("估值安全", scores.get("valuation_safety"))}
        </div>
      </div>
    </section>
    <section class="content">
      {queue_status_section}
      {signal_matrix_section}
      {render_valuation_chart(chart_runs, chart_prices)}
      {trackable_history_section}
      <section class="two-col">
        <div class="section-block">
          <h2>行业地位</h2>
          <p>{esc(latest.get('industry_position') or '等待个股深研入库。')}</p>
        </div>
        <div class="section-block">
          <h2>竞争格局</h2>
          <p>{esc(latest.get('competition_landscape') or '等待个股深研入库。')}</p>
        </div>
      </section>
      <section class="two-col">
        <div class="section-block">
          <h2>上下游公司</h2>
          <p>{esc(latest.get('upstream_downstream') or '等待个股深研入库。')}</p>
        </div>
        <div class="section-block">
          <h2>年增长率</h2>
          <p>{esc(latest.get('annual_growth') or '等待个股深研入库。')}</p>
        </div>
      </section>
      <section class="two-col">
        <div class="section-block">
          <h2>数倍潜力</h2>
          <p>{esc(latest.get('multi_bagger_potential') or '等待个股深研入库。')}</p>
        </div>
        <div class="section-block">
          <h2>重仓研究资格</h2>
          <p>{esc(decision_matrix.get('conclusion') or latest.get('heavy_position_view') or '等待个股深研入库。')}</p>
        </div>
      </section>
      <section class="section-block">
        <h2>本次触发原因</h2>
        <p>{esc(latest.get('trigger_reason') or (stock_queue[0]['trigger_reason'] if stock_queue else '等待个股深研入库。'))}</p>
      </section>
      <section class="section-block">
        <h2>风险与证伪</h2>
        <ul class="risk-list">{risk_items or '<li>等待个股深研入库。</li>'}</ul>
      </section>
      <section class="section-block">
        <h2>研究历史</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>日期</th><th>类型</th><th>触发原因</th><th>状态</th><th>估值方法</th><th>保守 / 合理 / 乐观</th><th>重仓资格</th></tr></thead>
            <tbody>{history_rows}</tbody>
          </table>
        </div>
      </section>
    </section>
"""
    return render_layout(f"{stock_name} {code}", body)


def api_stocks() -> bytes:
    with closing(connect(DB_PATH)) as conn:
        report = latest_report(conn)
        leaders = rows_to_dicts(list_latest_leaders(conn))
    return json.dumps({"report": dict(report) if report else None, "items": leaders}, ensure_ascii=False).encode("utf-8")


def api_index() -> bytes:
    with closing(connect(DB_PATH)) as conn:
        report = latest_report(conn)
        leaders = list_latest_leaders(conn)
    items = [leader_to_summary(row) for row in leaders]
    payload = {
        "schema_version": "myinveststock.index.v1",
        "page": {
            "title": "MyInvestStock",
            "primary_endpoint": "/api/index",
            "latest_endpoint": "/api/latest",
            "primary_result_path": "key_results.primary_output.items",
        },
        "source": {
            "leader_endpoint": LEADER_INDEX_URL,
            "leader_result_path": "key_results.primary_output.items",
            "theme_endpoint": THEME_INDEX_URL,
            "theme_context_paths": ["mainline_ranking", "legacy_theme_ranking", "market"],
            "source_policy": "Leader only supplies A trackable stock candidates; Theme supplies mainline environment and market context",
        },
        "report": dict(report) if report else None,
        "key_results": {
            "primary_output": {
                "title": "A可跟踪龙头",
                "count": len(items),
                "items": items,
            }
        },
        "links": {
            "web": "/",
            "latest": "/api/latest",
            "queue": "/api/queue",
            "stocks": "/api/stocks",
        },
        "constraints": {
            "read_only": True,
            "research_only": True,
            "contains_trade_orders": False,
            "contains_cash_amounts": False,
            "contains_share_counts": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def api_latest() -> bytes:
    with closing(connect(DB_PATH)) as conn:
        report = latest_report(conn)
        leaders = list_latest_leaders(conn)
        stocks = []
        research_run_count = 0
        valuation_run_count = 0
        for leader in leaders:
            runs = list_research_runs(conn, leader["code"])
            valuation_runs_for_stock = valuation_runs(conn, leader["code"])
            research_run_count += len(runs)
            valuation_run_count += len(valuation_runs_for_stock)
            latest_research = latest_stock_research(runs)
            leader_summary = leader_to_summary(leader)
            decision_matrix = decision_matrix_summary(
                leader_summary["upstream_signal"],
                latest_research["financial_signal"] if latest_research else financial_signal_summary(None),
            )
            stocks.append(
                {
                    "leader": leader_summary,
                    "research": {
                        "latest": latest_research,
                        "history": [research_run_to_summary(row) for row in runs],
                        "valuation_history": valuation_history_payload(valuation_runs_for_stock),
                        "run_count": len(runs),
                    },
                    "decision_matrix": decision_matrix,
                }
            )
    payload = {
        "schema_version": "myinveststock.research.v1",
        "report": dict(report) if report else None,
        "source": {
            "leader_endpoint": LEADER_INDEX_URL,
            "theme_endpoint": THEME_INDEX_URL,
            "theme_context_paths": ["mainline_ranking", "legacy_theme_ranking", "market"],
        },
        "summary": {
            "stock_count": len(stocks),
            "research_run_count": research_run_count,
            "valuation_run_count": valuation_run_count,
        },
        "stocks": stocks,
        "constraints": {
            "read_only": True,
            "research_only": True,
            "contains_trade_orders": False,
            "contains_cash_amounts": False,
            "contains_share_counts": False,
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def api_stock(code: str) -> bytes:
    with closing(connect(DB_PATH)) as conn:
        leader = get_latest_leader(conn, code) or get_known_leader(conn, code)
        runs = rows_to_dicts(list_research_runs(conn, code))
        queue = rows_to_dicts(list_queue_for_stock(conn, code))
        trackable = rows_to_dicts(list_trackable_history(conn, code))
    for row in queue:
        row["source_label"] = queue_source_label(row.get("source_type"))
    leader_summary = leader_to_summary(leader) if leader else None
    latest_run = next((row for row in runs if row.get("task_type") == "stock_research"), None)
    decision_matrix = decision_matrix_summary(
        leader_summary["upstream_signal"] if leader_summary else upstream_signal_summary(None),
        financial_signal_summary(latest_run) if latest_run else financial_signal_summary(None),
    )
    return json.dumps(
        {
            "leader": dict(leader) if leader else None,
            "leader_summary": leader_summary,
            "upstream_signal": leader_summary["upstream_signal"] if leader_summary else upstream_signal_summary(None),
            "research_runs": runs,
            "decision_matrix": decision_matrix,
            "queue": queue,
            "trackable_history": trackable,
        },
        ensure_ascii=False,
    ).encode("utf-8")


def api_queue() -> bytes:
    with closing(connect(DB_PATH)) as conn:
        rows = rows_to_dicts(list_queue(conn))
    for row in rows:
        row["source_label"] = queue_source_label(row.get("source_type"))
    return json.dumps({"items": rows}, ensure_ascii=False).encode("utf-8")


class MyInvestStockHandler(BaseHTTPRequestHandler):
    server_version = "MyInvestStock/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/":
            self.send_bytes(render_home(), "text/html; charset=utf-8")
            return
        if path == "/research":
            self.handle_research_gateway(parsed.query)
            return
        if path == "/api/index":
            self.send_bytes(api_index(), "application/json; charset=utf-8")
            return
        if path == "/api/latest":
            self.send_bytes(api_latest(), "application/json; charset=utf-8")
            return
        if path == "/api/stocks":
            self.send_bytes(api_stocks(), "application/json; charset=utf-8")
            return
        if path == "/api/queue":
            self.send_bytes(api_queue(), "application/json; charset=utf-8")
            return
        if path.startswith("/api/stocks/"):
            code = path.removeprefix("/api/stocks/").upper()
            self.send_bytes(api_stock(code), "application/json; charset=utf-8")
            return
        if path.startswith("/stocks/"):
            code = path.removeprefix("/stocks/").upper()
            self.send_bytes(render_stock_page(code), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            self.send_static(path.removeprefix("/static/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_research_gateway(self, query: str) -> None:
        code, requested_name = normalize_stock_query(parse_qs(query))
        if code is None or not STOCK_CODE_RE.match(code):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid stock code")
            return
        with closing(connect(DB_PATH)) as conn:
            exists, known_name = _stock_exists(conn, code)
        queued = False
        if not exists:
            enqueue_requested_stock(code, name=requested_name or known_name)
            queued = True
        location = f"/stocks/{quote(code)}"
        if queued:
            location += "?queued=1"
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, relative_path: str) -> None:
        safe = Path(relative_path)
        if safe.is_absolute() or ".." in safe.parts:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        path = ROOT / "web" / "static" / safe
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_bytes(path.read_bytes(), content_type)

    def log_message(self, format: str, *args: object) -> None:
        return


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer((host, port), MyInvestStockHandler)
    print(f"MyInvestStock Web running at http://{host}:{port}/", flush=True)
    httpd.serve_forever()
