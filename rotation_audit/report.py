"""Audit and repair reports.

Both commands share one :class:`~rotation_audit.model.Space`, so they always cut
the drill into the same segments and enumerate the same failure combinations --
even when a candidate action is never selected.

Repair works in four steps, each provably lossless:

1. **Baseline screening.**  Only scenarios that fail on the baseline can
   constrain the choice: adding copies never removes a usable copy, so
   recoverability is monotone in the selected action set.
2. **Class collapse.**  Scenarios with the same ``(baseline usable mask,
   candidate usable mask, required generations)`` behave identically under
   every action set, because a selected candidate becomes usable exactly where
   it appears in the candidate mask.  One representative per class suffices.
3. **Minimal fix sets.**  For every failing class, collect the
   *inclusion-minimal* action subsets (of size at most ``max_actions``) that
   make it recoverable.  By monotonicity a set fixes the class iff it contains
   one of them.  Candidates are resolved per scenario, so an action whose
   interval or fault domains miss the class contributes nothing there.
4. **Candidate reduction.**  Every feasible solution must contain a minimal fix
   set for every failing class, so some optimum lies inside their union ``U``.
   Enumerating the subsets of ``U`` in ``(cost, count, sorted IDs)`` order and
   returning the first feasible one is therefore exact; exhausting the
   enumeration is a proof of impossibility.
"""

from __future__ import annotations

import itertools
from typing import Dict, List, Optional, Sequence, Tuple

from .case import Case, GENERATIONS
from .model import Mask, Scenario, Space, build_witness, scenario_digest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def case_summary(case: Case, space: Space) -> dict:
    return {
        "format": "rotation-audit-v1",
        "drill": {
            "start": case.start,
            "dual_start": case.dual_start,
            "switch": case.switch,
            "dual_end": case.dual_end,
            "end": case.end,
        },
        "generations": {
            name: {
                "label": case.generations[name].label,
                "threshold": case.generations[name].threshold,
                "shares": list(case.generations[name].shares),
            }
            for name in GENERATIONS
        },
        "domains": list(case.domains),
        "budget": case.budget,
        "maxActions": case.max_actions,
        "copies": len(case.copies),
        "candidates": len(case.actions),
        "segments": len(space.segments),
        "faultSets": len(space.fault_sets),
        "scenarios": len(space.scenarios),
        "scenarioDigest": scenario_digest(space),
    }


def segment_list(space: Space) -> List[dict]:
    return [{"index": index, "start": lo, "end": hi}
            for index, (lo, hi) in enumerate(space.segments)]


def _entries(space: Space, usable_of) -> Tuple[List[dict], Optional[dict], int]:
    entries: List[dict] = []
    first_failure: Optional[dict] = None
    failures = 0
    cache: Dict[tuple, dict] = {}
    for scenario in space.scenarios:
        usable = usable_of(scenario)
        key = (usable, scenario.required)
        witness = cache.get(key)
        if witness is None:
            witness = build_witness(space, usable, scenario.required)
            cache[key] = witness
        entry = {
            "index": scenario.index,
            "segment": scenario.segment,
            "start": scenario.start,
            "end": scenario.end,
            "faults": list(scenario.faults),
            "requiredGenerations": list(scenario.required),
            "recoverable": witness["recoverable"],
            "copies": {generation: witness["copies"][generation]
                       for generation in scenario.required},
            "shortfall": ({} if witness["recoverable"] else
                          {generation: witness["shortfall"][generation]
                           for generation in scenario.required}),
        }
        entries.append(entry)
        if not witness["recoverable"]:
            failures += 1
            if first_failure is None:
                first_failure = entry
    return entries, first_failure, failures


def _summary(entries: Sequence[dict], first_failure: Optional[dict],
             failures: int) -> dict:
    return {
        "passed": failures == 0,
        "scenarioCount": len(entries),
        "recoverableScenarios": len(entries) - failures,
        "failedScenarios": failures,
        "firstFailure": first_failure,
    }


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def audit_report(case: Case) -> dict:
    space = Space(case)
    entries, first_failure, failures = _entries(
        space, lambda scenario: scenario.base_usable)
    return {
        "command": "audit",
        "case": case_summary(case, space),
        "segments": segment_list(space),
        "faultSets": [list(faults) for faults in space.fault_sets],
        "scenarios": entries,
        "summary": _summary(entries, first_failure, failures),
    }


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def _minimal_fixes(space: Space, scenario: Scenario,
                   usable_actions: Sequence[int]) -> List[Tuple[int, ...]]:
    """Inclusion-minimal action-index tuples making ``scenario`` recoverable.

    Subsets are generated by increasing size, so an existing fix can only be a
    strict subset of a later one; dropping those keeps every fix minimal and,
    because the predicate is monotone, drops no solving power.  Availability is
    resolved per scenario, so a candidate whose interval misses this segment
    simply contributes nothing here.
    """
    fixes: List[Tuple[int, ...]] = []
    failed: List[Mask] = []
    limit = min(space.case.max_actions, len(usable_actions))
    for size in range(1, limit + 1):
        for combo in itertools.combinations(usable_actions, size):
            if any(set(existing).issubset(combo) for existing in fixes):
                continue
            mask = space.usable_with(scenario, combo)
            # Dominance: if an earlier (hence smaller) action set already failed
            # with a superset of this availability, this set cannot succeed.
            if any(mask & previous == mask for previous in failed):
                continue
            if space.complete(mask, scenario.required):
                fixes.append(combo)
            else:
                failed.append(mask)
    return fixes


def repair_report(case: Case) -> dict:
    space = Space(case)
    action_count = len(case.actions)
    meta = case_summary(case, space)

    # -- step 1 + 2: failing behaviour classes ---------------------------
    classes: Dict[tuple, Scenario] = {}
    for scenario in space.scenarios:
        if space.complete(scenario.base_usable, scenario.required):
            continue
        classes.setdefault((scenario.base_usable, scenario.candidate_usable,
                            scenario.required), scenario)

    if not classes:
        entries, first_failure, failures = _entries(
            space, lambda scenario: scenario.base_usable)
        return {
            "command": "repair",
            "status": "feasible",
            "selected": [],
            "cost": 0,
            "actionCount": 0,
            "case": meta,
            "segments": segment_list(space),
            "faultSets": [list(faults) for faults in space.fault_sets],
            "scenarios": entries,
            "summary": _summary(entries, first_failure, failures),
        }

    # -- step 3: minimal fix sets ----------------------------------------
    minimal: Dict[tuple, List[Tuple[int, ...]]] = {}
    for key, scenario in classes.items():
        relevant = [index for index in range(action_count)
                    if scenario.candidate_usable & space.action_bits[index]]
        if not relevant:
            return _impossible(space, scenario, meta,
                               "the scenario has no candidate copy it could use")
        shifts = _minimal_fixes(space, scenario, relevant)
        if not shifts:
            return _impossible(
                space, scenario, meta,
                "no action set within maxActions makes this scenario recoverable")
        minimal[key] = shifts

    # -- step 4: reduce and search ---------------------------------------
    candidates = sorted({index for fixes in minimal.values()
                         for fix in fixes for index in fix})
    ordered: List[Tuple[int, int, Tuple[str, ...], frozenset]] = []
    for size in range(0, min(case.max_actions, len(candidates)) + 1):
        for combo in itertools.combinations(candidates, size):
            chosen = frozenset(combo)
            cost = sum(case.actions[index].cost for index in combo)
            ids = tuple(sorted(case.actions[index].id for index in combo))
            ordered.append((cost, size, ids, chosen))
    ordered.sort(key=lambda item: item[:3])

    for cost, size, ids, chosen in ordered:
        if all(any(set(fix).issubset(chosen) for fix in fixes)
               for fixes in minimal.values()):
            return _feasible(space, meta, list(ids), cost, chosen)

    representative = next(iter(classes.values()))
    return _impossible(
        space, representative, meta,
        "no action set within maxActions covers every failing scenario")


def _feasible(space: Space, meta: dict, ids: List[str], cost: int,
              chosen: frozenset) -> dict:
    selected = sorted(chosen)
    entries, first_failure, failures = _entries(
        space, lambda scenario: space.usable_with(scenario, selected))
    return {
        "command": "repair",
        "status": "feasible",
        "selected": ids,
        "cost": cost,
        "actionCount": len(ids),
        "case": meta,
        "segments": segment_list(space),
        "faultSets": [list(faults) for faults in space.fault_sets],
        "scenarios": entries,
        "summary": _summary(entries, first_failure, failures),
    }


def _impossible(space: Space, scenario: Scenario, meta: dict, reason: str) -> dict:
    return {
        "command": "repair",
        "status": "impossible",
        "selected": [],
        "cost": None,
        "actionCount": 0,
        "case": meta,
        "segments": segment_list(space),
        "faultSets": [list(faults) for faults in space.fault_sets],
        "reason": reason,
        "firstUnresolvable": scenario.to_json(),
        "scenarios": [],
        "summary": {
            "passed": False,
            "scenarioCount": len(space.scenarios),
            "recoverableScenarios": 0,
            "failedScenarios": len(space.scenarios),
            "firstFailure": None,
        },
    }
