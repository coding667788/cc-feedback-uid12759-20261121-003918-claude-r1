"""Key-custody rotation drill auditor (offline disaster-recovery exercise).

Public API::

    from rotation_audit import load, parse, audit_report, repair_report

``audit_report`` / ``repair_report`` take a parsed :class:`Case` and return a
JSON-serialisable ``dict``.  See ``README.md`` for the input/output schema.
"""

from .case import (Action, Case, CaseError, Copy, GENERATIONS, GEN_NEW, GEN_OLD,
                   Generation, load, loads, parse)
from .report import audit_report, repair_report

__all__ = [
    "Action",
    "Case",
    "CaseError",
    "Copy",
    "GEN_NEW",
    "GEN_OLD",
    "GENERATIONS",
    "Generation",
    "audit_report",
    "load",
    "loads",
    "parse",
    "repair_report",
]

__version__ = "1.0.0"
