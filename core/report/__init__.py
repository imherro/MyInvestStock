"""Deterministic stock research report assembly."""

from .assembler import REPORT_VERSION, build_stock_report, compute_report_hash
from .conclusion import ConclusionRuleResult, build_conclusion

__all__ = [
    "REPORT_VERSION",
    "ConclusionRuleResult",
    "build_conclusion",
    "build_stock_report",
    "compute_report_hash",
]
