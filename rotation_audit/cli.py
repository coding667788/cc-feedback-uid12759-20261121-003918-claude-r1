"""Command-line entry point: ``python -m rotation_audit {audit,repair} CASE.json``.

Exit codes

=====  ============================================================
0      audit: every scenario passes; repair: a feasible optimum was found
1      audit: at least one scenario fails; repair: proven impossible
2      usage error, unreadable file, or invalid input document
=====  ============================================================
"""

from __future__ import annotations

import json
import sys
from typing import List, Optional

from .case import CaseError, load
from .report import audit_report, repair_report

USAGE = "usage: python -m rotation_audit {audit,repair} CASE.json"


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2 or arguments[0] not in ("audit", "repair"):
        print(USAGE, file=sys.stderr)
        return 2
    command, path = arguments
    try:
        case = load(path)
        report = audit_report(case) if command == "audit" else repair_report(case)
    except CaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    if command == "audit":
        return 0 if report["summary"]["passed"] else 1
    return 0 if report["status"] == "feasible" else 1


if __name__ == "__main__":
    raise SystemExit(main())
