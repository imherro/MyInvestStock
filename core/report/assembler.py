from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any

from core.observability import TraceRecorder
from core.schema.stock_report import StockResearchReport
from core.task.state import compute_task_run_id
from core.valuation import (
    FundamentalFeatures,
    IntrinsicValueRange,
    PeerComparisonResult,
    PeerMetrics,
    ValuationSignal,
    build_valuation_signal,
    combine_value_ranges,
    compare_to_peers,
    extract_fundamental_features,
    pb_model_value,
    pe_model_value,
    simple_dcf_value,
)

from .conclusion import build_conclusion


REPORT_VERSION = "v1.0.0"
VALUATION_ENGINE_VERSION = "valuation_engine.v1"


def _safe_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: object, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: object) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else []


def _round_float(value: float) -> float:
    return round(float(value), 6)


def _canonical(value: object) -> object:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        return _round_float(value)
    return value


def compute_report_hash(
    *,
    stock_code: str,
    feature_inputs: object,
    valuation_outputs: object,
    peer_outputs: object,
) -> str:
    payload = {
        "report_version": REPORT_VERSION,
        "stock_code": stock_code,
        "feature_inputs": _canonical(feature_inputs),
        "valuation_outputs": _canonical(valuation_outputs),
        "peer_outputs": _canonical(peer_outputs),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _features_from_input(input_data: Mapping[str, Any]) -> tuple[FundamentalFeatures, list[Mapping[str, object]]]:
    rows = [row for row in _as_sequence(input_data.get("financial_rows")) if isinstance(row, Mapping)]
    feature_value = input_data.get("features")
    if isinstance(feature_value, FundamentalFeatures):
        return feature_value, rows
    if isinstance(feature_value, Mapping):
        return (
            FundamentalFeatures(
                revenue_growth_3y=_safe_float(feature_value.get("revenue_growth_3y")),
                profit_growth_3y=_safe_float(feature_value.get("profit_growth_3y")),
                roe_avg=_safe_float(feature_value.get("roe_avg")),
                gross_margin=_safe_float(feature_value.get("gross_margin")),
                debt_to_equity=_safe_float(feature_value.get("debt_to_equity")),
                fcf_yield=_safe_float(feature_value.get("fcf_yield")),
            ),
            rows,
        )
    return extract_fundamental_features(rows), rows


def _peers_from_input(input_data: Mapping[str, Any]) -> list[PeerMetrics]:
    peers: list[PeerMetrics] = []
    for raw_peer in _as_sequence(input_data.get("peers")):
        if not isinstance(raw_peer, Mapping):
            continue
        peers.append(
            PeerMetrics(
                stock_code=_safe_str(raw_peer.get("stock_code") or raw_peer.get("code") or raw_peer.get("name"), "unknown"),
                pe=_safe_float(raw_peer.get("pe")),
                roe=_safe_float(raw_peer.get("roe")),
            )
        )
    return peers


def _valuation_range(
    *,
    features: FundamentalFeatures,
    peer_result: PeerComparisonResult,
    valuation_inputs: Mapping[str, Any],
) -> IntrinsicValueRange:
    industry_pe = _safe_float(valuation_inputs.get("industry_pe"), peer_result.industry_median_pe)
    if industry_pe <= 0:
        industry_pe = _safe_float(valuation_inputs.get("stock_pe") or valuation_inputs.get("pe"))
    pe_range = pe_model_value(
        eps=_safe_float(valuation_inputs.get("eps")),
        industry_pe=industry_pe,
        relative_pe_score=_safe_float(valuation_inputs.get("relative_pe_score"), 1.0),
    )
    pb_range = pb_model_value(
        book_value_per_share=_safe_float(valuation_inputs.get("book_value_per_share")),
        roe=features.roe_avg,
        industry_pb=_safe_float(valuation_inputs.get("industry_pb"), _safe_float(valuation_inputs.get("pb"), 1.0)),
    )
    dcf_range = simple_dcf_value(
        fcf_per_share=_safe_float(valuation_inputs.get("fcf_per_share")),
        growth_rate=features.profit_growth_3y,
    )
    weights = [_safe_float(item) for item in _as_sequence(valuation_inputs.get("weights"))]
    return combine_value_ranges([pe_range, pb_range, dcf_range], weights=weights or [0.4, 0.3, 0.3])


def _fundamental_quality(features: FundamentalFeatures) -> dict[str, str]:
    revenue_quality = "收入增速较强" if features.revenue_growth_3y >= 0.15 else "收入增速平稳"
    profit_quality = "利润增速高于收入" if features.profit_growth_3y >= features.revenue_growth_3y else "利润增速未显著高于收入"
    cash_flow_quality = "自由现金流收益率为正" if features.fcf_yield > 0 else "自由现金流收益率不足"
    balance_sheet_quality = "资产负债压力较低" if features.debt_to_equity <= 1.0 else "杠杆水平需要跟踪"
    return {
        "revenue_quality": revenue_quality,
        "profit_quality": profit_quality,
        "cash_flow_quality": cash_flow_quality,
        "balance_sheet_quality": balance_sheet_quality,
    }


def _risk_block(features: FundamentalFeatures, input_data: Mapping[str, Any]) -> dict[str, object]:
    risk_input = _as_mapping(input_data.get("risk_signals") or input_data.get("risk"))
    financial_risk = _safe_str(
        risk_input.get("financial_risk"),
        "杠杆水平需要跟踪" if features.debt_to_equity > 1.0 else "财务风险主要来自增长和现金流波动",
    )
    industry_risk = _safe_str(risk_input.get("industry_risk"), "行业景气度和竞争强度变化可能影响估值中枢")
    sentiment_risk = _safe_str(risk_input.get("sentiment_risk"), "市场风险偏好变化可能影响短期估值")
    invalidation_conditions = [
        str(item).strip()
        for item in _as_sequence(risk_input.get("invalidation_conditions"))
        if str(item).strip()
    ]
    if not invalidation_conditions:
        invalidation_conditions = ["核心财务特征连续恶化", "相对同业竞争位置明显下滑"]
    return {
        "financial_risk": financial_risk,
        "industry_risk": industry_risk,
        "sentiment_risk": sentiment_risk,
        "invalidation_conditions": invalidation_conditions,
    }


def _industry_rank(peer_result: PeerComparisonResult, peer_count: int) -> int | None:
    if peer_count <= 0:
        return None
    rank = int((1.0 - peer_result.ranking_percentile) * (peer_count + 1)) + 1
    return max(1, min(peer_count + 1, rank))


def _valuation_confidence(peer_count: int, value_range: IntrinsicValueRange) -> str:
    if value_range.mid <= 0:
        return "low"
    if peer_count >= 3:
        return "high"
    return "medium"


def build_stock_report(input_data: Mapping[str, Any], trace_recorder: TraceRecorder | None = None) -> StockResearchReport:
    stock_code = _safe_str(input_data.get("stock_code") or input_data.get("code"), "000000.SH")
    stock_name = _safe_str(input_data.get("stock_name") or input_data.get("name"), stock_code)
    research_date = _safe_str(input_data.get("research_date"), "1970-01-01")
    task_type = _safe_str(input_data.get("task_type"), "financial")
    run_id = compute_task_run_id(stock_code, task_type, research_date, "stock_research_report.v1")

    features, financial_rows = _features_from_input(input_data)
    if trace_recorder is not None:
        trace_recorder.record(
            run_id=run_id,
            stage="feature",
            input_payload=financial_rows or input_data.get("features") or {},
            output_payload=features,
            diff_metrics={
                "revenue_growth_3y": _round_float(features.revenue_growth_3y),
                "profit_growth_3y": _round_float(features.profit_growth_3y),
                "roe_avg": _round_float(features.roe_avg),
            },
        )

    valuation_inputs = _as_mapping(input_data.get("valuation_inputs") or input_data.get("valuation"))
    peers = _peers_from_input(input_data)

    stock_pe = _safe_float(valuation_inputs.get("stock_pe") or valuation_inputs.get("pe"))
    peer_result = compare_to_peers(stock_pe=stock_pe, stock_roe=features.roe_avg, peers=peers)
    value_range = _valuation_range(features=features, peer_result=peer_result, valuation_inputs=valuation_inputs)
    if trace_recorder is not None:
        trace_recorder.record(
            run_id=run_id,
            stage="valuation",
            input_payload={
                "features": features,
                "valuation_inputs": valuation_inputs,
                "peers": peers,
            },
            output_payload={
                "value_range": value_range,
                "peer_result": peer_result,
            },
            diff_metrics={
                "pe": _round_float(stock_pe),
                "pb": _round_float(_safe_float(valuation_inputs.get("pb"))),
                "intrinsic_value_mid": _round_float(value_range.mid),
            },
        )
    signal = build_valuation_signal(
        current_price=_safe_float(valuation_inputs.get("current_price")),
        intrinsic_mid=value_range.mid,
        features=features,
        peer_comparison=peer_result,
    )
    if trace_recorder is not None:
        trace_recorder.record(
            run_id=run_id,
            stage="signal",
            input_payload={
                "current_price": _safe_float(valuation_inputs.get("current_price")),
                "intrinsic_mid": value_range.mid,
                "features": features,
                "peer_result": peer_result,
            },
            output_payload=signal,
            diff_metrics={
                "undervalued_score": _round_float(signal.undervalued_score),
                "growth_score": _round_float(signal.growth_score),
                "quality_score": _round_float(signal.quality_score),
                "risk_adjusted_score": _round_float(signal.risk_adjusted_score),
            },
        )
    conclusion = build_conclusion(signal)

    valuation_outputs = {
        "range": value_range,
        "signal": signal,
        "pe": stock_pe,
        "pb": _safe_float(valuation_inputs.get("pb")),
        "peg": _safe_float(valuation_inputs.get("peg"))
        or (stock_pe / (features.profit_growth_3y * 100.0) if stock_pe > 0 and features.profit_growth_3y > 0 else None),
    }
    peer_outputs = {"peer_result": peer_result, "peers": peers}
    report_hash = compute_report_hash(
        stock_code=stock_code,
        feature_inputs=financial_rows or features,
        valuation_outputs=valuation_outputs,
        peer_outputs=peer_outputs,
    )

    competitors = [peer.stock_code for peer in peers]
    qualities = _fundamental_quality(features)
    risk = _risk_block(features, input_data)
    evidence_items = [
        item for item in _as_sequence(input_data.get("evidence")) if isinstance(item, Mapping)
    ] or [
        {
            "source": "deterministic-report-assembler",
            "date": research_date,
            "url": "local",
            "purpose": "schema-first report assembly",
            "detail": "feature, valuation, peer and signal outputs assembled without LLM.",
        }
    ]

    payload = {
        "schema_version": "stock_research_report.v1",
        "report_version": REPORT_VERSION,
        "report_hash": report_hash,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "source_report_id": input_data.get("source_report_id"),
        "task_type": task_type,
        "research_date": research_date,
        "status": _safe_str(input_data.get("status"), "complete"),
        "title": _safe_str(input_data.get("title"), f"{stock_name}财务估值深研"),
        "summary": _safe_str(input_data.get("summary"), conclusion.summary),
        "industry_position": _safe_str(
            input_data.get("industry_position"),
            f"同业综合分位 {peer_result.ranking_percentile:.2f}，用于确定性行业位置判断。",
        ),
        "competition_landscape": _safe_str(
            input_data.get("competition_landscape"),
            f"同业样本：{', '.join(competitors) if competitors else '样本不足'}。",
        ),
        "upstream_downstream": _safe_str(
            input_data.get("upstream_downstream"),
            "上下游影响需以结构化供应链数据继续补充。",
        ),
        "annual_growth": _safe_str(
            input_data.get("annual_growth"),
            f"收入三年复合增速 {features.revenue_growth_3y:.2%}，利润三年复合增速 {features.profit_growth_3y:.2%}。",
        ),
        "multi_bagger_potential": _safe_str(
            input_data.get("multi_bagger_potential"),
            "五倍十倍潜力由增长分数、质量分数和估值安全边际共同约束。",
        ),
        "heavy_position_view": conclusion.grade,
        "fundamentals": {
            "revenue_growth": _round_float(features.revenue_growth_3y),
            "profit_growth": _round_float(features.profit_growth_3y),
            "roe": _round_float(features.roe_avg),
            "debt_ratio": _round_float(features.debt_to_equity),
            **qualities,
        },
        "valuation": {
            "pe": _round_float(stock_pe),
            "pb": _round_float(_safe_float(valuation_inputs.get("pb"))),
            "peg": _round_float(_safe_float(valuation_outputs["peg"])),
            "intrinsic_value_low": _round_float(value_range.low),
            "intrinsic_value_mid": _round_float(value_range.mid),
            "intrinsic_value_high": _round_float(value_range.high),
            "unit": _safe_str(valuation_inputs.get("unit"), "CNY/share"),
            "method": value_range.method,
            "confidence": _valuation_confidence(len(peers), value_range),
            "key_assumptions": [
                "valuation range generated by deterministic PE/PB/DCF engine",
                "valuation signal generated by deterministic scoring rules",
            ],
            "engine_version": VALUATION_ENGINE_VERSION,
            "undervalued_score": _round_float(signal.undervalued_score),
            "growth_score": _round_float(signal.growth_score),
            "quality_score": _round_float(signal.quality_score),
            "risk_adjusted_score": _round_float(signal.risk_adjusted_score),
        },
        "peer_comparison": {
            "industry_rank": _industry_rank(peer_result, len(peers)),
            "competitors": competitors,
            "relative_valuation": (
                f"stock PE {stock_pe:.2f}; industry median PE {peer_result.industry_median_pe:.2f}; "
                f"PE percentile {peer_result.pe_percentile:.2f}."
            ),
            "competitive_position": (
                f"stock ROE {features.roe_avg:.2%}; industry median ROE {peer_result.industry_median_roe:.2%}; "
                f"ranking percentile {peer_result.ranking_percentile:.2f}."
            ),
        },
        "risk": risk,
        "conclusion": {
            "grade": conclusion.grade,
            "confidence": conclusion.confidence,
            "summary": conclusion.summary,
        },
        "evidence": evidence_items,
        "assumptions": [
            "same input gives same StockResearchReport and report_hash",
            "LLM is not used during deterministic assembly",
        ],
    }
    report = StockResearchReport(**payload)
    if trace_recorder is not None:
        trace_recorder.record(
            run_id=run_id,
            stage="report",
            input_payload={
                "features": features,
                "valuation_outputs": valuation_outputs,
                "peer_outputs": peer_outputs,
                "risk": risk,
                "conclusion": conclusion,
            },
            output_payload=report.model_dump(mode="json"),
            diff_metrics={
                "stock_code": stock_code,
                "report_hash": report.report_hash,
                "pe": _round_float(stock_pe),
                "pb": _round_float(_safe_float(valuation_inputs.get("pb"))),
                "undervalued_score": _round_float(signal.undervalued_score),
                "risk_adjusted_score": _round_float(signal.risk_adjusted_score),
            },
        )
    return report
