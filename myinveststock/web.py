from __future__ import annotations

import html
import json
import mimetypes
import re
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import DB_PATH, DEFAULT_HOST, DEFAULT_PORT, FOOTER_SCRIPT_URL, ROOT, STATIC_ASSET_VERSION
from .db import (
    connect,
    get_latest_leader,
    latest_report,
    list_latest_leaders,
    list_queue,
    list_research_runs,
    rows_to_dicts,
    valuation_runs,
)
from .config import LEADER_INDEX_URL

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


def metric(label: str, value: object, unit: str = "") -> str:
    shown = fmt_num(value) if isinstance(value, (int, float)) else esc(value or "待入库")
    return f"""<div class="metric"><span>{esc(label)}</span><strong>{shown}{esc(unit)}</strong></div>"""


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
          <div class="stock-code">{esc(row['code'])}</div>
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

    queue_rows = "".join(
        f"""<tr>
      <td>{esc(row['priority'])}</td>
      <td>{esc(row['stage'])}</td>
      <td>{esc(row['code'])}</td>
      <td>{esc(row['name'])}</td>
      <td>{esc(row['task_type'])}</td>
      <td>{esc(row['status'])}</td>
      <td>{esc(row['task_keyword'])}</td>
    </tr>"""
        for row in queue
    )
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
          <thead><tr><th>优先级</th><th>阶段</th><th>代码</th><th>名称</th><th>类型</th><th>状态</th><th>任务关键词</th></tr></thead>
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


def render_valuation_chart(runs: list[object]) -> str:
    if not runs:
        return render_empty_section("合理估值区间历史")
    max_value = max(float(row["valuation_high"]) for row in runs if row["valuation_high"] is not None)
    rows = []
    for row in runs:
        low = float(row["valuation_low"])
        mid = float(row["valuation_mid"])
        high = float(row["valuation_high"])
        left = max(0.0, min(100.0, low / max_value * 100))
        width = max(2.0, min(100.0 - left, (high - low) / max_value * 100))
        mid_pos = max(0.0, min(100.0, mid / max_value * 100))
        rows.append(
            f"""<div class="valuation-row">
        <div class="valuation-date">{esc(row['research_date'])}</div>
        <div class="valuation-track">
          <span class="valuation-range" style="left:{left:.2f}%;width:{width:.2f}%"></span>
          <span class="valuation-mid" style="left:{mid_pos:.2f}%"></span>
        </div>
        <div class="valuation-label">{fmt_num(low)} / {fmt_num(mid)} / {fmt_num(high)}</div>
      </div>"""
        )
    return f"""<section class="section-block">
      <h2>合理估值区间历史</h2>
      <div class="valuation-chart">{''.join(rows)}</div>
    </section>"""


def render_stock_page(code: str) -> bytes:
    if not STOCK_CODE_RE.match(code):
        return render_layout("无效代码", "<section class=\"content\"><h1>无效股票代码</h1></section>")
    with closing(connect(DB_PATH)) as conn:
        leader = get_latest_leader(conn, code)
        runs = list_research_runs(conn, code)
        chart_runs = valuation_runs(conn, code)
        report = latest_report(conn)
    if leader is None:
        return render_layout("未找到", f"<section class=\"content\"><h1>未找到 {esc(code)}</h1></section>")

    market = load_json(leader["market_json"], {})
    scores = load_json(leader["scores_json"], {})
    risk_flags = load_json(leader["risk_flags_json"], [])
    strategic_run = next((dict(row) for row in runs if row["task_type"] == "strategic"), {})
    financial_run = next((dict(row) for row in runs if row["task_type"] == "financial"), {})
    latest = financial_run or strategic_run
    risks = load_json(latest.get("risks_json"), []) if latest else []
    risk_items = "".join(f"<li>{esc(item)}</li>" for item in (risks or risk_flags or []))

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
            <h1>{esc(leader['name'])}</h1>
            <p class="muted">{esc(leader['code'])} · {esc(leader['theme'])} · {esc(leader['candidate_leader_claim'])}</p>
          </div>
          <div class="report-box">
            <span>深研评级</span>
            <strong>{esc(leader['deep_rating'])} {esc(leader['deep_label'])}</strong>
            <span>{esc(report['basis_date'] if report else '')}</span>
          </div>
        </div>
        <div class="summary-grid">
          {metric("深研分", leader["deep_score"])}
          {metric("收盘", market.get("close"))}
          {metric("PE TTM", market.get("pe_ttm"))}
          {metric("PB", market.get("pb"))}
          {metric("证据质量", scores.get("evidence_quality"))}
          {metric("估值安全", scores.get("valuation_safety"))}
        </div>
      </div>
    </section>
    <section class="content">
      {render_valuation_chart(chart_runs)}
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
    return render_layout(f"{leader['name']} {leader['code']}", body)


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
        leader = get_latest_leader(conn, code)
        runs = rows_to_dicts(list_research_runs(conn, code))
    return json.dumps(
        {"leader": dict(leader) if leader else None, "research_runs": runs},
        ensure_ascii=False,
    ).encode("utf-8")


def api_queue() -> bytes:
    with closing(connect(DB_PATH)) as conn:
        rows = rows_to_dicts(list_queue(conn))
    return json.dumps({"items": rows}, ensure_ascii=False).encode("utf-8")


class MyInvestStockHandler(BaseHTTPRequestHandler):
    server_version = "MyInvestStock/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/":
            self.send_bytes(render_home(), "text/html; charset=utf-8")
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
