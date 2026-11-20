"""Strict parsing and validation of one drill description.

A document that survives :func:`parse` satisfies every invariant the rest of
the engine relies on, so the audit and repair code never re-validates.

Rejected inputs (every error raises :class:`CaseError` naming the JSON path):

* duplicate JSON keys at any nesting level;
* duplicate copy IDs, action IDs, per-generation share IDs, fault-domain IDs;
* duplicate fault domains on one copy;
* unknown references (a share of the other generation, an undeclared domain);
* JSON booleans or floats where an integer is required;
* empty, inverted, or out-of-window availability intervals;
* drill windows violating ``start <= dual_start < switch < dual_end <= end``;
* ``budget`` outside ``0..2`` and ``max_actions`` outside ``0..4``;
* non-positive thresholds and non-positive action costs;
* unknown object fields, so a typo can never silently change the scenario.

Overlapping, adjacent, and identical intervals on one copy are legal input:
they are unioned.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Tuple

FORMAT = "rotation-audit-v1"

GEN_OLD = "old"
GEN_NEW = "new"
GENERATIONS = (GEN_OLD, GEN_NEW)

MAX_FAILURE_BUDGET = 2
MAX_CHOSEN_ACTIONS = 4
MAX_DOMAINS = 10
MAX_CANDIDATES = 16
MAX_EXISTING_COPIES = 60
MAX_SHARES_PER_GENERATION = 10
MAX_SEGMENTS = 24

_IDENT = re.compile(r"[A-Za-z0-9_-]+")


class CaseError(ValueError):
    """The document is not a valid drill description."""


# ---------------------------------------------------------------------------
# Primitive checks
# ---------------------------------------------------------------------------


def _fail(path: str, message: str) -> None:
    raise CaseError(f"{path}: {message}")


def _object(value: Any, path: str, required: Sequence[str],
            optional: Sequence[str] = ()) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(path, "expected a JSON object")
    allowed = set(required) | set(optional)
    for key in value:
        if key not in allowed:
            _fail(path, f"unknown field {key!r}")
    for key in required:
        if key not in value:
            _fail(path, f"missing field {key!r}")
    return value


def _array(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        _fail(path, "expected a JSON array")
    return value


def _ident(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail(path, "expected a string identifier")
    if _IDENT.fullmatch(value) is None:
        _fail(path, f"identifier {value!r} must match [A-Za-z0-9_-]+")
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(path, "expected a JSON integer (booleans are not integers)")
    return value


def _bounded(value: Any, path: str, low: int, high: int) -> int:
    number = _integer(value, path)
    if not low <= number <= high:
        _fail(path, f"{number} outside the supported range [{low}, {high}]")
    return number


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(path, "expected a non-empty string")
    return value


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Copy:
    """One escrow copy: a share held by one custodian through some sites."""

    id: str
    generation: str
    share: str
    custodian: str
    domains: Tuple[str, ...]
    intervals: Tuple[Tuple[int, int], ...]

    def covers(self, instant: int) -> bool:
        """True when ``instant`` belongs to the union of this copy's intervals."""
        return any(lo <= instant < hi for lo, hi in self.intervals)

    def union_intervals(self) -> Tuple[Tuple[int, int], ...]:
        """Sorted, merged union; adjacent/overlapping intervals collapse."""
        merged: List[List[int]] = []
        for lo, hi in sorted(self.intervals):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        return tuple((lo, hi) for lo, hi in merged)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "generation": self.generation,
            "share": self.share,
            "custodian": self.custodian,
            "domains": list(self.domains),
            "intervals": [[lo, hi] for lo, hi in self.intervals],
        }


@dataclass(frozen=True)
class Action:
    """A pre-approved move that adds one complete copy."""

    id: str
    cost: int
    copy: Copy

    def to_json(self) -> dict:
        return {"id": self.id, "cost": self.cost, "copy": self.copy.to_json()}


@dataclass(frozen=True)
class Generation:
    name: str
    label: str
    threshold: int
    shares: Tuple[str, ...]


@dataclass(frozen=True)
class Case:
    start: int
    dual_start: int
    switch: int
    dual_end: int
    end: int
    budget: int
    max_actions: int
    generations: Dict[str, Generation]
    domains: Tuple[str, ...]
    copies: Tuple[Copy, ...]
    actions: Tuple[Action, ...]

    def threshold(self, generation: str) -> int:
        return self.generations[generation].threshold

    def copies_by_id(self) -> Dict[str, Copy]:
        return {copy.id: copy for copy in self.copies}

    def actions_by_id(self) -> Dict[str, Action]:
        return {action.id: action for action in self.actions}

    def all_copy_specs(self) -> Tuple[Copy, ...]:
        """Existing copies plus candidate copies, sorted by copy ID."""
        return tuple(sorted([copy for copy in self.copies]
                            + [action.copy for action in self.actions],
                            key=lambda copy: copy.id))


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------


def loads(text: str) -> Any:
    """``json.loads`` that rejects duplicate object keys at every depth."""
    try:
        return json.loads(text, object_pairs_hook=_pairs_to_dict)
    except json.JSONDecodeError as error:
        raise CaseError(f"invalid JSON: {error}") from None


def _pairs_to_dict(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CaseError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------


def parse(document: Any) -> Case:
    document = _object(document, "case",
                       ("drill", "generations", "domains", "copies", "actions",
                        "budget", "max_actions"),
                       ("format",))

    if "format" in document and document["format"] != FORMAT:
        _fail("format", f"expected {FORMAT!r}")

    raw_drill = _object(document["drill"], "drill",
                        ("start", "dual_start", "switch", "dual_end", "end"))
    start = _integer(raw_drill["start"], "drill.start")
    dual_start = _integer(raw_drill["dual_start"], "drill.dual_start")
    switch = _integer(raw_drill["switch"], "drill.switch")
    dual_end = _integer(raw_drill["dual_end"], "drill.dual_end")
    end = _integer(raw_drill["end"], "drill.end")
    if not (start <= dual_start < switch < dual_end <= end):
        _fail("drill", "expected start <= dual_start < switch < dual_end <= end")

    raw_generations = _object(document["generations"], "generations", GENERATIONS)
    generations: Dict[str, Generation] = {}
    for name in GENERATIONS:
        path = f"generations.{name}"
        raw = _object(raw_generations[name], path, ("threshold", "shares"), ("label",))
        label = _text(raw.get("label", name), f"{path}.label")
        threshold = _integer(raw["threshold"], f"{path}.threshold")
        if threshold <= 0:
            _fail(f"{path}.threshold", "must be a positive integer")
        shares: List[str] = []
        seen_shares: set = set()
        for position, raw_share in enumerate(_array(raw["shares"], f"{path}.shares")):
            share = _ident(raw_share, f"{path}.shares[{position}]")
            if share in seen_shares:
                _fail(f"{path}.shares[{position}]", f"duplicate share ID {share!r}")
            seen_shares.add(share)
            shares.append(share)
        if not shares:
            _fail(f"{path}.shares", "at least one share is required")
        if len(shares) > MAX_SHARES_PER_GENERATION:
            _fail(f"{path}.shares",
                  f"at most {MAX_SHARES_PER_GENERATION} shares per generation")
        if threshold > len(shares):
            _fail(f"{path}.threshold", "exceeds the declared share count")
        shares.sort()
        generations[name] = Generation(name, label, threshold, tuple(shares))

    domains: List[str] = []
    seen_domains: set = set()
    for position, raw_domain in enumerate(_array(document["domains"], "domains")):
        domain = _ident(raw_domain, f"domains[{position}]")
        if domain in seen_domains:
            _fail(f"domains[{position}]", f"duplicate fault domain {domain!r}")
        seen_domains.add(domain)
        domains.append(domain)
    if len(domains) > MAX_DOMAINS:
        _fail("domains", f"at most {MAX_DOMAINS} fault domains are supported")
    domains.sort()

    budget = _bounded(document["budget"], "budget", 0, MAX_FAILURE_BUDGET)
    max_actions = _bounded(document["max_actions"], "max_actions", 0,
                           MAX_CHOSEN_ACTIONS)

    def parse_copy(raw: Any, path: str) -> Copy:
        copy_id = _ident(raw["id"], f"{path}.id")
        generation = raw["generation"]
        if not isinstance(generation, str) or generation not in GENERATIONS:
            _fail(f"{path}.generation", "expected 'old' or 'new'")
        share = _ident(raw["share"], f"{path}.share")
        if share not in generations[generation].shares:
            _fail(f"{path}.share", f"unknown {generation}-generation share {share!r}")
        custodian = _ident(raw["custodian"], f"{path}.custodian")
        copy_domains: List[str] = []
        seen_copy_domains: set = set()
        for position, raw_domain in enumerate(_array(raw["domains"], f"{path}.domains")):
            domain = _ident(raw_domain, f"{path}.domains[{position}]")
            if domain not in seen_domains:
                _fail(f"{path}.domains[{position}]", f"unknown fault domain {domain!r}")
            if domain in seen_copy_domains:
                _fail(f"{path}.domains[{position}]",
                      f"duplicate fault domain {domain!r} on one copy")
            seen_copy_domains.add(domain)
            copy_domains.append(domain)
        raw_intervals = _array(raw["intervals"], f"{path}.intervals")
        if not raw_intervals:
            _fail(f"{path}.intervals",
                  "at least one availability interval is required")
        intervals: List[Tuple[int, int]] = []
        for position, raw_interval in enumerate(raw_intervals):
            interval_path = f"{path}.intervals[{position}]"
            pair = _array(raw_interval, interval_path)
            if len(pair) != 2:
                _fail(interval_path, "expected a [from, until] pair")
            lo = _integer(pair[0], f"{interval_path}[0]")
            hi = _integer(pair[1], f"{interval_path}[1]")
            if lo >= hi:
                _fail(interval_path,
                      "empty or inverted interval (from must be < until)")
            if lo < start or hi > end:
                _fail(interval_path, "interval lies outside the drill window")
            intervals.append((lo, hi))
        intervals.sort()
        return Copy(copy_id, generation, share, custodian,
                    tuple(sorted(copy_domains)), tuple(intervals))

    copies: List[Copy] = []
    seen_copy_ids: set = set()
    for position, raw in enumerate(_array(document["copies"], "copies")):
        path = f"copies[{position}]"
        raw = _object(raw, path,
                      ("id", "generation", "share", "custodian", "domains",
                       "intervals"))
        copy = parse_copy(raw, path)
        if copy.id in seen_copy_ids:
            _fail(f"{path}.id", f"duplicate copy ID {copy.id!r}")
        seen_copy_ids.add(copy.id)
        copies.append(copy)
    if len(copies) > MAX_EXISTING_COPIES:
        _fail("copies", f"at most {MAX_EXISTING_COPIES} existing copies are supported")

    actions: List[Action] = []
    seen_action_ids: set = set()
    for position, raw in enumerate(_array(document["actions"], "actions")):
        path = f"actions[{position}]"
        raw = _object(raw, path, ("id", "cost", "copy"))
        action_id = _ident(raw["id"], f"{path}.id")
        if action_id in seen_action_ids:
            _fail(f"{path}.id", f"duplicate action ID {action_id!r}")
        seen_action_ids.add(action_id)
        cost = _integer(raw["cost"], f"{path}.cost")
        if cost <= 0:
            _fail(f"{path}.cost", "must be a positive integer")
        copy_path = f"{path}.copy"
        raw_copy = _object(raw["copy"], copy_path,
                           ("id", "generation", "share", "custodian", "domains",
                            "intervals"))
        copy = parse_copy(raw_copy, copy_path)
        if copy.id in seen_copy_ids:
            _fail(f"{copy_path}.id",
                  f"candidate copy ID {copy.id!r} collides with another copy")
        seen_copy_ids.add(copy.id)
        actions.append(Action(action_id, cost, copy))
    if len(actions) > MAX_CANDIDATES:
        _fail("actions", f"at most {MAX_CANDIDATES} candidate actions are supported")

    copies.sort(key=lambda copy: copy.id)
    actions.sort(key=lambda action: action.id)

    # The segment count is a property of the input alone, so reject it here
    # rather than when a command first builds the scenario space.
    points = {start, dual_start, switch, dual_end, end}
    for copy in copies:
        for lo, hi in copy.intervals:
            points.add(lo)
            points.add(hi)
    for action in actions:
        for lo, hi in action.copy.intervals:
            points.add(lo)
            points.add(hi)
    ordered = sorted(point for point in points if start <= point <= end)
    segment_count = sum(1 for lo, hi in zip(ordered, ordered[1:]) if lo < hi)
    if segment_count > MAX_SEGMENTS:
        _fail("drill", f"interval endpoints split the drill into {segment_count} "
                       f"segments; at most {MAX_SEGMENTS} are supported")

    return Case(start=start, dual_start=dual_start, switch=switch, dual_end=dual_end,
                end=end, budget=budget, max_actions=max_actions,
                generations=generations, domains=tuple(domains),
                copies=tuple(copies), actions=tuple(actions))


def load(path: str) -> Case:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise CaseError(f"cannot read {path}: {error}") from None
    return parse(loads(text))
