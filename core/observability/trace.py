from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from typing import Any


TRACE_STAGES = ("feature", "valuation", "signal", "report")


@dataclass(frozen=True)
class TraceEvent:
    run_id: str
    stage: str
    input_hash: str
    output_hash: str
    timestamp: str
    diff_metrics: dict[str, Any]


def _canonical(value: object) -> object:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, float):
        return round(float(value), 6)
    return value


def hash_payload(payload: object) -> str:
    encoded = json.dumps(
        _canonical(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class TraceRecorder:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def record(
        self,
        *,
        run_id: str,
        stage: str,
        input_payload: object,
        output_payload: object,
        diff_metrics: dict[str, Any] | None = None,
    ) -> TraceEvent:
        if stage not in TRACE_STAGES:
            raise ValueError(f"unsupported trace stage: {stage}")
        event = TraceEvent(
            run_id=run_id,
            stage=stage,
            input_hash=hash_payload(input_payload),
            output_hash=hash_payload(output_payload),
            timestamp=_utc_timestamp(),
            diff_metrics=dict(diff_metrics or {}),
        )
        self.events.append(event)
        return event
