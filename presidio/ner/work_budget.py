"""Process-local monotonic budget shared by queues, windows and HTTP phases."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import math
import threading
import time

BUDGET_HEADER = "X-Ru-LLM-Analysis-Budget-Seconds"
MAX_BUDGET_SECONDS = 86_400.0


@dataclass(frozen=True)
class WorkBudget:
    deadline: float
    cancellation: threading.Event | None = None


CURRENT_BUDGET: ContextVar[WorkBudget | None] = ContextVar("ner_work_budget", default=None)


def parse_budget(value: str | float) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_BUDGET_SECONDS:
        raise ValueError("invalid analysis budget")
    return seconds


def remaining_seconds() -> float | None:
    budget = CURRENT_BUDGET.get()
    return (None if budget is None or not math.isfinite(budget.deadline)
            else max(0.0, budget.deadline - time.monotonic()))


def check_work() -> None:
    from .triton_recognizer import NERInferenceCancelled, NERProcessingError
    budget = CURRENT_BUDGET.get()
    if budget is None:
        return
    if budget.cancellation is not None and budget.cancellation.is_set():
        raise NERInferenceCancelled("NER inference was cancelled")
    if budget.deadline <= time.monotonic():
        raise NERProcessingError(phase="inference", failure_class="analysis_deadline_exceeded")


@contextmanager
def work_scope(*, seconds: float | None = None, cancellation=None):
    parent = CURRENT_BUDGET.get()
    deadline = parent.deadline if parent is not None else float("inf")
    if seconds is not None:
        deadline = min(deadline, time.monotonic() + parse_budget(seconds))
    event = cancellation if cancellation is not None else (parent.cancellation if parent else None)
    token = CURRENT_BUDGET.set(WorkBudget(deadline, event))
    try:
        check_work()
        yield
    finally:
        CURRENT_BUDGET.reset(token)
