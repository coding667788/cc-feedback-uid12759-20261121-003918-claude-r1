"""Independent brute-force oracle used only by the test suite.

This module deliberately shares nothing with :mod:`rotation_audit` except the
parsed :class:`~rotation_audit.case.Case` data model: it re-cuts the timeline,
re-enumerates failure sets, enumerates *every* valid selection of copies per
scenario, and enumerates *every* action subset.  It is exponential and only
intended for small cases; comparing it with the real engine on generated cases
is the strongest correctness check available.
"""

from __future__ import annotations

import itertools
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from rotation_audit.case import Case, Copy, GEN_NEW, GEN_OLD

Witness = Tuple[Tuple[str, ...], ...]


def segments(case: Case) -> List[Tuple[int, int]]:
    points = {case.start, case.dual_start, case.switch, case.dual_end, case.end}
    for copy in case.all_copy_specs():
        for lo, hi in copy.intervals:
            points.add(lo)
            points.add(hi)
    ordered = sorted(point for point in points if case.start <= point <= case.end)
    return [(lo, hi) for lo, hi in zip(ordered, ordered[1:]) if lo < hi]


def fault_sets(case: Case) -> List[Tuple[str, ...]]:
    combos = [combo
              for size in range(0, case.budget + 1)
              for combo in itertools.combinations(sorted(case.domains), size)]
    combos.sort(key=lambda combo: (len(combo), combo))
    return combos


def required(case: Case, start: int) -> Tuple[str, ...]:
    """Required generations, old before new, per the reporting order."""
    result = []
    if start < case.dual_end or start < case.switch:
        result.append(GEN_OLD)
    if start >= case.dual_start or start >= case.switch:
        result.append(GEN_NEW)
    return tuple(result)


def usable_copies(case: Case, start: int, faults: Sequence[str],
                  extra: Sequence[Copy] = ()) -> List[Copy]:
    result = []
    for copy in list(case.copies) + list(extra):
        if not copy.covers(start):
            continue
        if set(copy.domains) & set(faults):
            continue
        result.append(copy)
    return result


def _subsets_of_size(items: Sequence[Copy], size: int) -> Iterable[Tuple[Copy, ...]]:
    for combo in itertools.combinations(items, size):
        shares = [copy.share for copy in combo]
        if len(set(shares)) != len(shares):
            continue
        yield combo


def all_partial_witnesses(case: Case, picks: Sequence[Copy],
                          required_gens: Sequence[str]) -> List[Witness]:
    """Every valid selection: ``<= threshold`` per generation, distinct shares and people."""
    thresholds = [case.generations[g].threshold for g in required_gens]
    per_generation: List[List[Tuple[Copy, ...]]] = []
    for generation, limit in zip(required_gens, thresholds):
        pool = [copy for copy in picks if copy.generation == generation]
        choices: List[Tuple[Copy, ...]] = [()]
        for size in range(1, limit + 1):
            choices.extend(_subsets_of_size(pool, size))
        per_generation.append(choices)

    result: List[Witness] = []
    for combo in itertools.product(*per_generation):
        people = [copy.custodian for group in combo for copy in group]
        if len(set(people)) != len(people):
            continue
        result.append(tuple(tuple(sorted(copy.id for copy in group)) for group in combo))
    return result


def best_witness(case: Case, picks: Sequence[Copy],
                 required_gens: Sequence[str]) -> Optional[Witness]:
    """Maximal-total, then lexicographically smallest nested list.

    Returns ``None`` when no witness at all exists (which only happens if a
    required generation has zero usable copies).
    """
    candidates = all_partial_witnesses(case, picks, required_gens)
    if not candidates:
        return None
    best_total = max(sum(len(group) for group in witness) for witness in candidates)
    return min((witness for witness in candidates
                if sum(len(group) for group in witness) == best_total))


def scenario_rows(case: Case, extra: Sequence[Copy] = ()) -> List[dict]:
    """Reference audit rows in the engine's exact order."""
    rows = []
    for start, end in segments(case):
        required_gens = required(case, start)
        for faults in fault_sets(case):
            picks = usable_copies(case, start, faults, extra)
            witness = best_witness(case, picks, required_gens)
            thresholds = [case.generations[g].threshold for g in required_gens]
            if witness is None:
                copies = {g: [] for g in required_gens}
                chosen = 0
                total = 0
            else:
                copies = {g: list(witness[slot]) for slot, g in enumerate(required_gens)}
                total = sum(len(group) for group in witness)
                chosen = total
            target = sum(thresholds)
            rows.append({
                "start": start,
                "end": end,
                "faults": list(faults),
                "required": list(required_gens),
                "recoverable": total == target,
                "copies": copies,
                "shortfall": {} if total == target else {
                    g: thresholds[slot] - len(copies[g])
                    for slot, g in enumerate(required_gens)},
            })
    return rows


def all_pass(case: Case, extra: Sequence[Copy] = ()) -> bool:
    return all(row["recoverable"] for row in scenario_rows(case, extra))


def best_repair(case: Case) -> Optional[Tuple[int, int, Tuple[str, ...]]]:
    """Exhaustive optimum over every action subset of size ``<= max_actions``."""
    best: Optional[Tuple[int, int, Tuple[str, ...]]] = None
    indices = range(len(case.actions))
    for size in range(0, case.max_actions + 1):
        for combo in itertools.combinations(indices, size):
            added = [case.actions[index].copy for index in combo]
            if not all_pass(case, added):
                continue
            cost = sum(case.actions[index].cost for index in combo)
            ids = tuple(sorted(case.actions[index].id for index in combo))
            candidate = (cost, size, ids)
            if best is None or candidate < best:
                best = candidate
    return best
