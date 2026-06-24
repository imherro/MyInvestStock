from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    RETRY = "RETRY"


ALLOWED_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.BLOCKED},
    TaskStatus.RUNNING: {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED},
    TaskStatus.FAILED: {TaskStatus.RETRY},
    TaskStatus.RETRY: {TaskStatus.PENDING},
    TaskStatus.BLOCKED: {TaskStatus.PENDING, TaskStatus.FAILED},
    TaskStatus.DONE: set(),
}


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    run_id: str
    stock_code: str
    task_type: str
    status: TaskStatus
    retry_count: int
    created_at: str
    updated_at: str
    error_message: str | None = None


def compute_task_run_id(stock_code: str, task_type: str, task_date: str, schema_version: str) -> str:
    seed = "|".join([stock_code.upper(), task_type, task_date, schema_version])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def compute_task_id(run_id: str) -> str:
    return f"task_{run_id}"


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if current == target:
        return
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid task transition: {current.value} -> {target.value}")
