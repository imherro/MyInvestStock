from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from .config import RAW_DATA_DIR, THEME_INDEX_URL


def fetch_theme_index(url: str = THEME_INDEX_URL, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MyInvestStock/0.1 (+theme context bridge)"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read().decode("utf-8")
    return json.loads(data)


def theme_report_meta(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    latest = result.get("latest_report") if isinstance(result.get("latest_report"), dict) else {}
    report_id = latest.get("report_id") or result.get("report_id")
    if not report_id:
        raise ValueError("Missing theme report_id")
    return {
        "report_id": report_id,
        "basis_date": latest.get("basis_date") or result.get("basis_date"),
        "generated_at": latest.get("generated_at") or result.get("generated_at"),
        "data_quality_status": latest.get("data_quality_status")
        or (result.get("data_quality_summary") or {}).get("status"),
        "contract_validation_status": latest.get("contract_validation_status")
        or (result.get("contract_validation_summary") or {}).get("status"),
    }


def save_theme_payload(payload: dict[str, Any], report_id: str, raw_dir: Path = RAW_DATA_DIR) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    safe_report_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", report_id)
    path = raw_dir / f"{safe_report_id}.theme_index.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _result_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = payload.get("result")
    return result if isinstance(result, dict) else payload


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unique_texts(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def theme_keys(row: dict[str, Any]) -> list[str]:
    return _unique_texts([row.get("theme"), row.get("theme_name"), row.get("theme_id")])


def _rows_by_theme(rows: list[Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in theme_keys(row):
            result[key] = row
    return result


def market_context_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    result = _result_payload(payload)
    market = _as_dict(result.get("market"))
    breadth = _as_dict(market.get("breadth") or result.get("breadth"))
    indexes = _as_list(market.get("broad_indexes") or result.get("broad_indexes"))
    up_ratio = _float(breadth.get("up_ratio"))
    r5_positive_ratio = _float(breadth.get("r5_positive_ratio"))
    r20_positive_ratio = _float(breadth.get("r20_positive_ratio"))
    index_r5_values = [_float(row.get("r5")) for row in indexes if isinstance(row, dict)]
    index_r20_values = [_float(row.get("r20")) for row in indexes if isinstance(row, dict)]
    valid_r5 = [value for value in index_r5_values if value is not None]
    valid_r20 = [value for value in index_r20_values if value is not None]
    avg_r5 = round(sum(valid_r5) / len(valid_r5), 4) if valid_r5 else None
    avg_r20 = round(sum(valid_r20) / len(valid_r20), 4) if valid_r20 else None

    if up_ratio is None and avg_r5 is None:
        state = "市场状态缺失"
        risk_appetite = "待确认"
    elif (up_ratio or 0.0) >= 55.0 and (avg_r5 or 0.0) >= 1.0:
        state = "指数与宽度偏强"
        risk_appetite = "偏积极"
    elif (up_ratio or 100.0) <= 45.0 and (avg_r5 or 0.0) < 0.0:
        state = "指数与宽度偏弱"
        risk_appetite = "偏谨慎"
    else:
        state = "结构性震荡"
        risk_appetite = "结构性中性"

    return {
        "source": "MyInvestTheme /api/index market",
        "market_state": state,
        "risk_appetite": risk_appetite,
        "breadth": {
            "up_ratio": up_ratio,
            "r5_positive_ratio": r5_positive_ratio,
            "r20_positive_ratio": r20_positive_ratio,
            "median_pct_chg": breadth.get("median_pct_chg"),
            "median_r5": breadth.get("median_r5"),
            "median_r20": breadth.get("median_r20"),
        },
        "broad_index_average": {
            "r5": avg_r5,
            "r20": avg_r20,
        },
        "broad_indexes": [
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "r1": row.get("r1"),
                "r5": row.get("r5"),
                "r20": row.get("r20"),
            }
            for row in indexes[:5]
            if isinstance(row, dict)
        ],
    }


def _crowding_signal(mainline: dict[str, Any], legacy: dict[str, Any]) -> str:
    if mainline.get("cycle_stage") == "crowded_late":
        return "高位拥挤期"
    scores = [
        _float(legacy.get("market_score")),
        _float(legacy.get("evidence_score")),
        _float(legacy.get("ths_score")),
        _float(legacy.get("etf_score")),
        _float(mainline.get("cycle_market_score")),
        _float(mainline.get("cycle_evidence_score")),
    ]
    if any(value is not None and value >= 85.0 for value in scores):
        return "热度拥挤代理偏高"
    if any(value is not None and value >= 72.0 for value in scores):
        return "热度较高但未到拥挤"
    return "未显示高位拥挤"


def _mainline_bucket(context: dict[str, Any]) -> str:
    stage = str(context.get("cycle_stage") or "")
    score = _float(context.get("mainline_score_v6"))
    market_score = _float(context.get("cycle_market_score") or context.get("market_score"))
    if stage in {"main_rise_diffusion", "launch_confirmation"}:
        return "strong"
    if stage == "policy_incubation":
        return "watch" if (score or 0.0) >= 0.2 else "weak"
    if stage == "crowded_late":
        return "watch"
    if stage in {"cooling_decline", "legacy_residual", "not_active"}:
        return "weak"
    if score is not None and score >= 1.0 and (market_score or 0.0) >= 50.0:
        return "strong"
    if score is not None and score >= 0.3:
        return "watch"
    if score is not None:
        return "weak"
    return "unknown"


def theme_context_for(themes: list[Any] | str | None, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    result = _result_payload(payload)
    if not result:
        return None
    names = _unique_texts([themes] if isinstance(themes, str) else list(themes or []))
    mainline_by_theme = _rows_by_theme(_as_list(result.get("mainline_ranking")))
    legacy_by_theme = _rows_by_theme(_as_list(result.get("legacy_theme_ranking") or result.get("theme_ranking")))
    matched_name = next((name for name in names if name in mainline_by_theme or name in legacy_by_theme), "")
    if not matched_name:
        return None
    mainline = dict(mainline_by_theme.get(matched_name) or {})
    legacy = dict(legacy_by_theme.get(matched_name) or {})
    latest = _as_dict(result.get("latest_report"))
    meta = theme_report_meta(result)
    market_context = market_context_summary(result)
    context = {
        "source": "MyInvestTheme /api/index",
        "source_endpoint": THEME_INDEX_URL,
        "report_id": meta.get("report_id"),
        "basis_date": meta.get("basis_date"),
        "generated_at": meta.get("generated_at"),
        "data_quality_status": meta.get("data_quality_status"),
        "contract_validation_status": meta.get("contract_validation_status"),
        "policy_provenance_status": latest.get("policy_provenance_status")
        or _as_dict(result.get("policy_provenance_summary")).get("status"),
        "snapshot_status": latest.get("policy_snapshot_status")
        or _as_dict(result.get("policy_snapshot_summary")).get("status"),
        "theme": mainline.get("theme_name") or legacy.get("theme") or matched_name,
        "theme_id": mainline.get("theme_id") or legacy.get("theme_id"),
        "mainline_score_v6": mainline.get("mainline_score_v6"),
        "lifecycle_state": mainline.get("lifecycle_state"),
        "lifecycle_state_label": mainline.get("lifecycle_state_label") or mainline.get("lifecycle_state"),
        "cycle_stage": mainline.get("cycle_stage"),
        "cycle_stage_label": mainline.get("cycle_stage_label"),
        "cycle_time_window": mainline.get("cycle_time_window"),
        "cycle_stage_advice": mainline.get("cycle_stage_advice"),
        "cycle_market_score": mainline.get("cycle_market_score"),
        "cycle_evidence_score": mainline.get("cycle_evidence_score"),
        "score_30d": mainline.get("score_30d"),
        "score_90d": mainline.get("score_90d"),
        "event_count_30d": mainline.get("event_count_30d"),
        "event_count_90d": mainline.get("event_count_90d"),
        "source_org_count_90d": mainline.get("source_org_count_90d"),
        "supportive_cluster_count": mainline.get("supportive_cluster_count"),
        "restrictive_cluster_count": mainline.get("restrictive_cluster_count"),
        "market_score": legacy.get("market_score"),
        "evidence_score": legacy.get("evidence_score"),
        "policy_score": legacy.get("policy_score"),
        "etf_score": legacy.get("etf_score"),
        "ths_score": legacy.get("ths_score"),
        "sw_score": legacy.get("sw_score"),
        "limit_count": legacy.get("limit_count"),
        "large_net": legacy.get("large_net"),
        "top_etf": legacy.get("top_etf"),
        "top_ths": legacy.get("top_ths"),
        "top_sw": legacy.get("top_sw"),
        "market_context": market_context,
    }
    context["crowding_signal"] = _crowding_signal(mainline, legacy)
    context["market_state"] = market_context.get("market_state")
    context["risk_appetite"] = market_context.get("risk_appetite")
    context["bucket"] = _mainline_bucket(context)
    return context


def enrich_leader_item(item: dict[str, Any], theme_payload: dict[str, Any] | None) -> dict[str, Any]:
    themes = [item.get("theme"), *list(item.get("themes") or [])]
    context = theme_context_for(themes, theme_payload)
    if not context:
        return dict(item)
    return {**item, "theme_context": context}
