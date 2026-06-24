from __future__ import annotations

import html
import json
import mimetypes
import re
from contextlib import closing
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import DB_PATH, DEFAULT_HOST, DEFAULT_PORT, FOOTER_SCRIPT_URL, ROOT, STATIC_ASSET_VERSION
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
from .config import LEADER_INDEX_URL
from .leader_index import enqueue_requested_stock

STOCK_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


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
    return {
        "id": row["id"],
        "task_type": row["task_type"],
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
        "evidence": load_json(row["evidence_json"], []),
        "assumptions": load_json(row["assumptions_json"], []),
        "risks": load_json(row["risks_json"], []),
    }


def latest_by_task_type(runs: list[object], task_type: str) -> dict[str, object] | None:
    for row in runs:
        if row["task_type"] == task_type:
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
  <header class="app-header">
    <a class="brand" href="/">MyInvestStock</a>
    <nav class="top-nav">
      <a href="/">A可跟踪龙头</a>
      <a href="/api/queue">研究队列</a>
    </nav>
  </header>
  <main>
{body}
  </main>
  <script src="{FOOTER_SCRIPT_URL}" defer></script>
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
        return '<tr><td colspan="8" class="empty-cell">当前队列为空。</td></tr>'
    return "".join(
        f"""<tr>
      <td>{esc(row['priority'])}</td>
      <td>{esc(row['stage'])}</td>
      <td>{esc(queue_source_label(row['source_type']))}</td>
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
          <thead><tr><th>优先级</th><th>阶段</th><th>来源</th><th>代码</th><th>名称</th><th>类型</th><th>状态</th><th>任务关键词</th></tr></thead>
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


def _valuation_price_start(runs: list[object]) -> str | None:
    dates = [_parsed_date(_row_value(row, "research_date")) for row in runs]
    valid_dates = [item for item in dates if item is not None]
    if not valid_dates:
        return None
    return (min(valid_dates) - timedelta(days=45)).isoformat()


def _render_plain_valuation_chart(points: list[dict[str, object]]) -> str:
    if not points:
        return render_empty_section("合理估值区间历史")

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
      <h2>合理估值区间历史</h2>
      <div class="valuation-chart">
        <svg class="valuation-history-svg" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="合理估值区间随时间变化图">
          <title>合理估值区间随时间变化图</title>
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
          <span><i class="legend-band"></i>保守-乐观区间</span>
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


def _render_kline_valuation_chart(
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

    price_lows = [float(item["low"]) for item in price_points]
    price_highs = [float(item["high"]) for item in price_points]
    valuation_lows = [float(item["low"]) for item in valuation_points]
    valuation_highs = [float(item["high"]) for item in valuation_points]
    lower = min(price_lows + valuation_lows)
    upper = max(price_highs + valuation_highs)
    span = upper - lower
    pad = max(span * 0.08, max(abs(upper), 1.0) * 0.02, 1.0)
    y_min = lower - pad
    y_max = upper + pad

    price_dates = [_parsed_date(item["date"]) for item in price_points]
    price_count = len(price_points)
    spacing = plot_width / (price_count - 1)
    candle_width = min(max(spacing * 0.58, 2.4), 7.5)

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

    candles = []
    label_step = max(1, (price_count + 5) // 6)
    x_labels = []
    for index, item in enumerate(price_points):
        x = _chart_x(index, price_count, left, plot_width)
        y_open = _chart_y(float(item["open"]), y_min, y_max, top, plot_height)
        y_close = _chart_y(float(item["close"]), y_min, y_max, top, plot_height)
        y_high = _chart_y(float(item["high"]), y_min, y_max, top, plot_height)
        y_low = _chart_y(float(item["low"]), y_min, y_max, top, plot_height)
        body_top = min(y_open, y_close)
        body_height = max(abs(y_close - y_open), 1.4)
        trend_class = "kline-up" if float(item["close"]) >= float(item["open"]) else "kline-down"
        tooltip = (
            f"{item['date']} | 开 {fmt_num(item['open'])} | 高 {fmt_num(item['high'])} | "
            f"低 {fmt_num(item['low'])} | 收 {fmt_num(item['close'])}"
        )
        candles.append(
            f"""<g class="kline-candle {trend_class}">
          <title>{esc(tooltip)}</title>
          <line class="kline-wick" x1="{x:.1f}" y1="{y_high:.1f}" x2="{x:.1f}" y2="{y_low:.1f}"></line>
          <rect class="kline-body" x="{x - candle_width / 2:.1f}" y="{body_top:.1f}" width="{candle_width:.1f}" height="{body_height:.1f}"></rect>
        </g>"""
        )
        if index % label_step == 0 or index == price_count - 1:
            x_labels.append(
                f"""<text class="valuation-date-label" x="{x:.1f}" y="{height - 18:.1f}" text-anchor="middle">{esc(short_date(item['date']))}</text>"""
            )

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
      <h2>合理估值区间历史</h2>
      <div class="valuation-chart">
        <svg class="valuation-history-svg" viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="K线叠加合理估值区间图">
          <title>K线叠加合理估值区间图</title>
          <line class="valuation-axis-line" x1="{left:.1f}" y1="{top:.1f}" x2="{left:.1f}" y2="{plot_bottom:.1f}"></line>
          <line class="valuation-axis-line" x1="{left:.1f}" y1="{plot_bottom:.1f}" x2="{plot_right:.1f}" y2="{plot_bottom:.1f}"></line>
          <text class="valuation-axis-title" x="{left:.1f}" y="16" text-anchor="start">价格 CNY/share</text>
          <text class="valuation-range-label" x="{plot_right:.1f}" y="16" text-anchor="end">{esc(short_date(first_date))} - {esc(short_date(last_date))}</text>
          {''.join(tick_lines)}
          <g class="kline-layer">{''.join(candles)}</g>
          <g class="valuation-overlay-layer">
            {''.join(bands)}
            {''.join(boundary_lines)}
            {''.join(mid_lines)}
            {''.join(markers)}
          </g>
          {''.join(x_labels)}
        </svg>
        <div class="valuation-legend">
          <span><i class="legend-kline"></i>近期K线</span>
          <span><i class="legend-band"></i>保守-乐观区间</span>
          <span><i class="legend-line"></i>合理估值中枢</span>
          <span><i class="legend-dot"></i>财务深研刷新点</span>
        </div>
      </div>
    </section>"""


def render_valuation_chart(runs: list[object], prices: list[object] | None = None) -> str:
    points = _valuation_chart_points(runs)
    if not points:
        return render_empty_section("合理估值区间历史")
    price_points = _daily_price_points(prices or [])
    if price_points:
        return _render_kline_valuation_chart(points, price_points)
    return _render_plain_valuation_chart(points)


def render_stock_queue_status(queue: list[object]) -> str:
    if not queue:
        return ""
    rows = "".join(
        f"""<tr>
      <td>{esc(row['task_type'])}</td>
      <td>{esc(queue_source_label(row['source_type']))}</td>
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
            <thead><tr><th>类型</th><th>来源</th><th>状态</th><th>任务关键词</th><th>更新时间</th></tr></thead>
            <tbody>{rows}</tbody>
          </table>
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
    if leader is not None:
        return True, str(leader["name"])
    runs = list_research_runs(conn, code)
    if runs:
        return True, str(runs[0]["name"])
    queue = list_queue_for_stock(conn, code)
    if queue:
        return True, str(queue[0]["name"])
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
        price_start = _valuation_price_start(chart_runs)
        chart_prices = list_daily_prices(conn, code, start_date=price_start, limit=260) if price_start else []
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
    strategic_run = next((dict(row) for row in runs if row["task_type"] == "strategic"), {})
    financial_run = next((dict(row) for row in runs if row["task_type"] == "financial"), {})
    latest = financial_run or strategic_run
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
    rating_label = (
        f"{leader['deep_rating'] or ''} {leader['deep_label'] or ''}".strip()
        if leader is not None
        else (queue_source_label(stock_queue[0]["source_type"]) if stock_queue else "待研究")
    )
    report_date = report["basis_date"] if report else ""
    queue_status_section = render_stock_queue_status(stock_queue)
    trackable_history_section = render_trackable_history(trackable_history)

    history_rows = "".join(
        f"""<tr>
      <td>{esc(row['research_date'])}</td>
      <td>{esc(row['task_type'])}</td>
      <td>{esc(row['status'])}</td>
      <td>{esc(row['valuation_method'] or '待入库')}</td>
      <td>{fmt_num(row['valuation_low'])} / {fmt_num(row['valuation_mid'])} / {fmt_num(row['valuation_high'])}</td>
      <td>{esc(row['heavy_position_view'] or '待入库')}</td>
    </tr>"""
        for row in runs
    )
    if not history_rows:
        history_rows = "<tr><td colspan=\"6\" class=\"empty-cell\">等待个股深研入库。</td></tr>"

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
      {render_valuation_chart(chart_runs, chart_prices)}
      {trackable_history_section}
      <section class="two-col">
        <div class="section-block">
          <h2>行业地位</h2>
          <p>{esc(strategic_run.get('industry_position') or '等待战略深研入库。')}</p>
        </div>
        <div class="section-block">
          <h2>竞争格局</h2>
          <p>{esc(strategic_run.get('competition_landscape') or '等待战略深研入库。')}</p>
        </div>
      </section>
      <section class="two-col">
        <div class="section-block">
          <h2>上下游公司</h2>
          <p>{esc(strategic_run.get('upstream_downstream') or '等待战略深研入库。')}</p>
        </div>
        <div class="section-block">
          <h2>年增长率</h2>
          <p>{esc(financial_run.get('annual_growth') or '等待财务估值深研入库。')}</p>
        </div>
      </section>
      <section class="two-col">
        <div class="section-block">
          <h2>五倍十倍潜力</h2>
          <p>{esc((financial_run or strategic_run).get('multi_bagger_potential') or '等待深研入库。')}</p>
        </div>
        <div class="section-block">
          <h2>重仓研究资格</h2>
          <p>{esc(financial_run.get('heavy_position_view') or '等待财务估值深研入库。')}</p>
        </div>
      </section>
      <section class="section-block">
        <h2>风险与证伪</h2>
        <ul class="risk-list">{risk_items or '<li>等待个股深研入库。</li>'}</ul>
      </section>
      <section class="section-block">
        <h2>研究历史</h2>
        <div class="table-wrap">
          <table>
            <thead><tr><th>日期</th><th>类型</th><th>状态</th><th>估值方法</th><th>保守 / 合理 / 乐观</th><th>重仓资格</th></tr></thead>
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
            "upstream_endpoint": LEADER_INDEX_URL,
            "upstream_result_path": "key_results.primary_output.items",
            "source_policy": "only A trackable leaders; do not expand from upstream /api/latest themes[].stock_leaders",
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
            strategic = latest_by_task_type(runs, "strategic")
            financial = latest_by_task_type(runs, "financial")
            stocks.append(
                {
                    "leader": leader_to_summary(leader),
                    "research": {
                        "strategic": strategic,
                        "financial": financial,
                        "latest": financial or strategic,
                        "valuation_history": valuation_history_payload(valuation_runs_for_stock),
                        "run_count": len(runs),
                    },
                }
            )
    payload = {
        "schema_version": "myinveststock.research.v1",
        "report": dict(report) if report else None,
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
    return json.dumps(
        {
            "leader": dict(leader) if leader else None,
            "research_runs": runs,
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
            enqueue_requested_stock(code, name=requested_name or known_name or code)
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
