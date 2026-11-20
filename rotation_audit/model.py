"""Scenario space and exact recovery witnesses.

A *scenario* is one minimal time segment crossed with one enabled failure set.
Availability is constant on a segment (every copy interval endpoint is a cut
point), so a scenario reduces to a bitmask of usable copies plus the tuple of
generations that must be recovered.

**Flow oracle.**  A witness selects copies so that no required generation
supplies more than ``threshold`` of them, share IDs are distinct inside a
generation, and custodians are distinct across the whole witness.  Model a
network ``source -> generation -> (generation, share) -> custodian -> sink``
where a generation edge carries its threshold and every middle edge carries one.
The flow value is then exactly the largest number of copies any witness can
hold, and a flow equal to the sum of thresholds is a complete witness.  The
partial witness the statement asks for is precisely the one that reaches this
flow value.

**Lexicographic witness.**  A witness is written as nested lists of copy IDs,
each list ascending, old before new.  Two facts make the minimum easy to build:

* every list of a *complete* witness has a fixed length, so plain
  lexicographic order decides and the minimum is built left to right by taking
  the smallest copy that still belongs to some witness;
* a *partial* witness trades length for content, and nested-list order treats a
  prefix as smaller than every extension, so the number of picks per generation
  is part of the choice.  There are at most ``threshold_old + 1`` feasible
  splits of the maximal total, so every split is tried and the best nested
  list wins.

"Belongs to some witness" is answered by one max flow plus a residual SCC pass
(a copy edge is on some maximum flow iff it is saturated, or its endpoints share
a residual component), so no replacement set is ever enumerated.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .case import Case, Copy, GEN_NEW, GEN_OLD, GENERATIONS
from .flow import Dinic

Mask = int
ShareKey = Tuple[str, str]


@dataclass(frozen=True)
class Scenario:
    index: int
    segment: int
    start: int
    end: int
    faults: Tuple[str, ...]
    required: Tuple[str, ...]
    usable: Mask       # usable copies of required generations (baseline + candidates)
    base_usable: Mask  # usable copies of required generations that already exist

    @property
    def candidate_usable(self) -> Mask:
        """Usable candidate copies: the part of ``usable`` that is not baseline."""
        return self.usable & ~self.base_usable

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "segment": self.segment,
            "start": self.start,
            "end": self.end,
            "faults": list(self.faults),
            "requiredGenerations": list(self.required),
        }


class Space:
    """Segmentation, failure combinations, and the copy universe of one case."""

    def __init__(self, case: Case) -> None:
        self.case = case
        self.copies: Tuple[Copy, ...] = case.all_copy_specs()
        self.size = len(self.copies)
        self.position: Dict[str, int] = {copy.id: index
                                         for index, copy in enumerate(self.copies)}
        existing_ids = case.copies_by_id()
        existing_mask = 0
        for index, copy in enumerate(self.copies):
            if copy.id in existing_ids:
                existing_mask |= 1 << index
        self.existing_mask = existing_mask
        self.action_bits: Tuple[int, ...] = tuple(1 << self.position[action.copy.id]
                                                  for action in case.actions)
        # Per (scenario, action) availability: a selected candidate only becomes
        # usable where its own interval and fault domains allow it.
        self._available: Dict[Tuple[int, int], bool] = {}
        self.action_index: Dict[str, int] = {action.id: index
                                             for index, action in enumerate(case.actions)}
        self.thresholds: Dict[str, int] = {name: case.generations[name].threshold
                                           for name in GENERATIONS}

        self.segments: Tuple[Tuple[int, int], ...] = self._segments()
        self.fault_sets: Tuple[Tuple[str, ...], ...] = self._fault_sets()
        self.scenarios: Tuple[Scenario, ...] = self._scenarios()
        self._instances: Dict[tuple, "_Instance"] = {}

    # -- construction ----------------------------------------------------

    def _segments(self) -> Tuple[Tuple[int, int], ...]:
        case = self.case
        points = {case.start, case.dual_start, case.switch, case.dual_end, case.end}
        for copy in self.copies:
            for lo, hi in copy.intervals:
                points.add(lo)
                points.add(hi)
        ordered = sorted(point for point in points if case.start <= point <= case.end)
        return tuple((lo, hi) for lo, hi in zip(ordered, ordered[1:]) if lo < hi)

    def _fault_sets(self) -> Tuple[Tuple[str, ...], ...]:
        domains = self.case.domains
        combos = [combo
                  for size in range(0, self.case.budget + 1)
                  for combo in itertools.combinations(domains, size)]
        combos.sort(key=lambda combo: (len(combo), combo))
        return tuple(combos)

    def required_at(self, start: int) -> Tuple[str, ...]:
        """Generations required on the segment starting at ``start``.

        The three rules of the statement -- old before ``switch``, new from
        ``switch``, both inside ``[dual_start, dual_end)`` -- collapse to two
        closed-form tests because ``dual_start < switch < dual_end``:

        * old is required exactly on ``[start, dual_end)``,
        * new is required exactly on ``[dual_start, end)``.

        A segment never crosses one of the five drill instants, so its start
        alone decides.
        """
        case = self.case
        required = []
        if start < case.dual_end:
            required.append(GEN_OLD)
        if start >= case.dual_start:
            required.append(GEN_NEW)
        return tuple(required)

    def _scenarios(self) -> Tuple[Scenario, ...]:
        time_masks: List[int] = []
        for lo, _ in self.segments:
            mask = 0
            for index, copy in enumerate(self.copies):
                if copy.covers(lo):
                    mask |= 1 << index
            time_masks.append(mask)
        domain_masks: List[int] = []
        for faults in self.fault_sets:
            blocked = set(faults)
            mask = 0
            for index, copy in enumerate(self.copies):
                if not blocked.intersection(copy.domains):
                    mask |= 1 << index
            domain_masks.append(mask)

        scenarios: List[Scenario] = []
        for segment, (lo, hi) in enumerate(self.segments):
            required = self.required_at(lo)
            generation_mask = 0
            for index, copy in enumerate(self.copies):
                if copy.generation in required:
                    generation_mask |= 1 << index
            for faults, domain_mask in zip(self.fault_sets, domain_masks):
                usable = time_masks[segment] & domain_mask & generation_mask
                scenarios.append(Scenario(len(scenarios), segment, lo, hi, faults,
                                          required, usable,
                                          usable & self.existing_mask))
        return tuple(scenarios)

    # -- oracle ----------------------------------------------------------

    def action_usable(self, scenario: Scenario, action: int) -> bool:
        """Whether the candidate ``action`` is usable in ``scenario``."""
        key = (scenario.index, action)
        cached = self._available.get(key)
        if cached is None:
            cached = bool(scenario.candidate_usable & self.action_bits[action])
            self._available[key] = cached
        return cached

    def usable_with(self, scenario: Scenario, selected: Sequence[int]) -> Mask:
        """Baseline copies plus the selected candidates that are usable here."""
        mask = scenario.base_usable
        for action in selected:
            if self.action_usable(scenario, action):
                mask |= self.action_bits[action]
        return mask

    def target(self, required: Sequence[str]) -> int:
        return sum(self.thresholds[generation] for generation in required)

    def instance(self, usable: Mask, required: Sequence[str],
                 caps: Optional[Sequence[int]] = None,
                 blocked_shares: FrozenSet[ShareKey] = frozenset(),
                 blocked_people: FrozenSet[str] = frozenset()) -> "_Instance":
        required = tuple(required)
        cap_tuple = (tuple(self.thresholds[generation] for generation in required)
                     if caps is None else tuple(caps))
        key = (usable, required, cap_tuple, blocked_shares, blocked_people)
        cached = self._instances.get(key)
        if cached is None:
            cached = _Instance(self, usable, required, cap_tuple,
                               blocked_shares, blocked_people)
            self._instances[key] = cached
        return cached

    def flow(self, usable: Mask, required: Sequence[str],
             caps: Optional[Sequence[int]] = None) -> int:
        return self.instance(usable, required, caps).flow

    def complete(self, usable: Mask, required: Sequence[str]) -> bool:
        return self.flow(usable, required) == self.target(required)


class _Instance:
    """One max-flow instance: usable copies, per-generation caps, blocked axes."""

    __slots__ = ("space", "required", "caps", "flow", "target", "graph",
                 "copy_edges", "components")

    def __init__(self, space: Space, usable: Mask, required: Sequence[str],
                 caps: Sequence[int], blocked_shares: FrozenSet[ShareKey],
                 blocked_people: FrozenSet[str]) -> None:
        self.space = space
        self.required = tuple(required)
        self.caps = tuple(caps)
        self.target = sum(self.caps)
        self.copy_edges: List[Tuple[int, int, int, List[int]]] = []
        self.components: List[int] = []
        self.graph: Optional[Dinic] = None
        self.flow = 0
        if self.target > 0:
            self._build(usable, blocked_shares, blocked_people)

    def _build(self, usable: Mask, blocked_shares: FrozenSet[ShareKey],
               blocked_people: FrozenSet[str]) -> None:
        cap_of = {generation: cap
                  for generation, cap in zip(self.required, self.caps) if cap > 0}
        share_nodes: Dict[ShareKey, int] = {}
        person_nodes: Dict[str, int] = {}
        picks: List[Tuple[int, int, int]] = []
        mask = usable
        while mask:
            lowest = mask & -mask
            index = lowest.bit_length() - 1
            mask ^= lowest
            copy = self.space.copies[index]
            if copy.generation not in cap_of:
                continue
            share_key = (copy.generation, copy.share)
            if share_key in blocked_shares or copy.custodian in blocked_people:
                continue
            if share_key not in share_nodes:
                share_nodes[share_key] = len(share_nodes)
            if copy.custodian not in person_nodes:
                person_nodes[copy.custodian] = len(person_nodes)
            picks.append((index, share_nodes[share_key], person_nodes[copy.custodian]))

        active = [generation for generation in self.required if cap_of.get(generation, 0) > 0]
        source, sink = 0, 1
        generation_node = {generation: 2 + slot
                           for slot, generation in enumerate(active)}
        share_base = 2 + len(active)
        person_base = share_base + len(share_nodes)
        graph = Dinic(person_base + len(person_nodes))
        for generation in active:
            graph.add(source, generation_node[generation], cap_of[generation])
        for (generation, _share), node in share_nodes.items():
            graph.add(generation_node[generation], share_base + node, 1)
        for node in range(len(person_nodes)):
            graph.add(person_base + node, sink, 1)
        copy_edges: List[Tuple[int, int, int, List[int]]] = []
        for index, share_node, person_node in picks:
            edge = graph.add(share_base + share_node, person_base + person_node, 1)
            copy_edges.append((index, share_base + share_node,
                               person_base + person_node, edge))
        self.flow = graph.max_flow(source, sink)
        self.graph = graph
        self.copy_edges = copy_edges
        if self.flow == self.target:
            self.components = graph.residual_scc()

    def feasible(self) -> bool:
        return self.flow == self.target

    def selectable(self, generation: str) -> List[int]:
        """Copies of ``generation`` that occur in some maximum flow, ascending.

        A saturated edge is on the current maximum flow.  An unsaturated one is
        on some maximum flow iff a residual path runs from its head back to its
        tail, which the SCC labelling detects.
        """
        if self.flow != self.target or self.target == 0:
            return []
        component = self.components
        result: List[int] = []
        for index, share_node, person_node, edge in self.copy_edges:
            if self.space.copies[index].generation != generation:
                continue
            if edge[1] == 0 or component[share_node] == component[person_node]:
                result.append(index)
        result.sort()
        return result


# ---------------------------------------------------------------------------
# Witness construction
# ---------------------------------------------------------------------------


def _greedy(space: Space, usable: Mask, required: Sequence[str],
            caps: Sequence[int]) -> Optional[List[List[int]]]:
    """Lexicographically smallest witness with exactly ``caps`` picks per generation.

    Returns ``None`` when the caps are not jointly achievable.  Each accepted
    copy is backed by a max-flow test, so the greedy never backtracks.
    """
    required = tuple(required)
    caps = list(caps)
    blocked_shares: Set[ShareKey] = set()
    blocked_people: Set[str] = set()
    chosen: List[List[int]] = [[] for _ in required]
    for slot, generation in enumerate(required):
        while caps[slot] > 0:
            instance = space.instance(usable, required, caps,
                                      frozenset(blocked_shares),
                                      frozenset(blocked_people))
            if not instance.feasible():
                return None
            candidates = instance.selectable(generation)
            if not candidates:
                return None
            index = candidates[0]
            copy = space.copies[index]
            chosen[slot].append(index)
            caps[slot] -= 1
            blocked_shares.add((copy.generation, copy.share))
            blocked_people.add(copy.custodian)
    return chosen


def build_witness(space: Space, usable: Mask, required: Sequence[str]) -> dict:
    """Lexicographically smallest witness; complete when one exists.

    Returns ``{"copies": {gen: [ids]}, "recoverable": bool, "shortfall": {gen: n}}``.
    """
    required = tuple(required)
    thresholds = [space.thresholds[generation] for generation in required]
    total = space.flow(usable, required)
    complete = total == space.target(required)

    best_key: Optional[List[List[str]]] = None
    best_lists: Optional[List[List[int]]] = None
    for split in _splits(thresholds, total):
        if space.flow(usable, required, split) != total:
            continue
        lists = _greedy(space, usable, required, split)
        if lists is None:
            continue
        key = [[space.copies[index].id for index in ids] for ids in lists]
        if best_key is None or key < best_key:
            best_key = key
            best_lists = lists

    if best_lists is None:
        # ``total`` is always achievable by the thresholds split; this is only
        # reachable if the flow oracle and the greedy disagree, which would be
        # a bug, so fail loudly rather than emit a wrong report.
        raise AssertionError("witness construction lost the maximum witness")

    copies = {generation: sorted(space.copies[index].id for index in best_lists[slot])
              for slot, generation in enumerate(required)}
    shortfall = {generation: thresholds[slot] - len(best_lists[slot])
                 for slot, generation in enumerate(required)}
    return {"copies": copies, "recoverable": complete, "shortfall": shortfall}


def _splits(thresholds: Sequence[int], total: int) -> Iterable[List[int]]:
    """Every distribution of ``total`` picks with ``0 <= picks_i <= threshold_i``."""
    if not thresholds:
        if total == 0:
            yield []
        return

    def walk(slot: int, left: int, prefix: List[int]) -> Iterable[List[int]]:
        if slot == len(thresholds) - 1:
            if 0 <= left <= thresholds[slot]:
                yield prefix + [left]
            return
        remaining_capacity = sum(thresholds[slot + 1:])
        low = max(0, left - remaining_capacity)
        high = min(thresholds[slot], left)
        for picks in range(low, high + 1):
            yield from walk(slot + 1, left - picks, prefix + [picks])

    if 0 <= total <= sum(thresholds):
        yield from walk(0, total, [])


def scenario_digest(space: Space) -> str:
    """Stable fingerprint of the segment/fault grid both commands share."""
    payload = {
        "segments": [list(segment) for segment in space.segments],
        "faultSets": [list(faults) for faults in space.fault_sets],
        "domains": list(space.case.domains),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
