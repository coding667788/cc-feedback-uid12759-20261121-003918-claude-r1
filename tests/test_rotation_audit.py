"""Tests for the rotation drill auditor.

Run with::

    python -m unittest discover -s tests -v

The last class compares the engine against ``tests/oracle.py``, an independent
brute-force implementation that enumerates every witness and every action
subset.  It shares no solving code with the package.
"""

from __future__ import annotations

import copy as copy_module
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from rotation_audit import CaseError, audit_report, parse, repair_report  # noqa: E402
from rotation_audit.case import loads  # noqa: E402
from rotation_audit.model import Space  # noqa: E402

import oracle  # noqa: E402  (tests directory on sys.path)


# ---------------------------------------------------------------------------
# Case builders
# ---------------------------------------------------------------------------


def make_case(*, drill=(0, 4, 5, 6, 10), domains=(), budget=0, max_actions=4,
              old_shares=("o1", "o2"), new_shares=("n1", "n2"),
              old_threshold=2, new_threshold=2,
              copies=(), actions=()):
    """Build a document dict; copies/actions are dicts without boilerplate."""
    return {
        "format": "rotation-audit-v1",
        "drill": {
            "start": drill[0],
            "dual_start": drill[1],
            "switch": drill[2],
            "dual_end": drill[3],
            "end": drill[4],
        },
        "generations": {
            "old": {"label": "old", "threshold": old_threshold, "shares": list(old_shares)},
            "new": {"label": "new", "threshold": new_threshold, "shares": list(new_shares)},
        },
        "domains": list(domains),
        "budget": budget,
        "max_actions": max_actions,
        "copies": list(copies),
        "actions": list(actions),
    }


def copy(copy_id, generation, share, custodian, domains=(), intervals=((0, 10),)):
    return {"id": copy_id, "generation": generation, "share": share,
            "custodian": custodian, "domains": list(domains),
            "intervals": [list(interval) for interval in intervals]}


def action(action_id, cost, **copy_fields):
    return {"id": action_id, "cost": cost, "copy": copy_fields}


def sample_document():
    return make_case(
        copies=[
            copy("old-a", "old", "o1", "A"),
            copy("old-b", "old", "o2", "B"),
            copy("new-a", "new", "n1", "A"),
            copy("new-c", "new", "n2", "C"),
        ],
        actions=[
            action("action-1", 10, id="new-d-1", generation="new", share="n1",
                   custodian="D", domains=(), intervals=[[4, 6]]),
            action("action-2", 10, id="new-d-2", generation="new", share="n1",
                   custodian="D", domains=(), intervals=[[4, 6]]),
            action("action-3", 3, id="new-d-3", generation="new", share="n1",
                   custodian="D", domains=(), intervals=[[5, 6]]),
        ],
    )


def run(document):
    case = parse(loads(json.dumps(document)))
    return case, audit_report(case), repair_report(case)


def scenario_rows(report):
    return report["scenarios"]


# ---------------------------------------------------------------------------
# Sample case: the behaviour the statement pins down
# ---------------------------------------------------------------------------


class SampleCaseTest(unittest.TestCase):
    def setUp(self):
        self.case, self.audit, self.repair = run(sample_document())

    def test_segments(self):
        self.assertEqual(self.audit["segments"],
                         [{"index": 0, "start": 0, "end": 4},
                          {"index": 1, "start": 4, "end": 5},
                          {"index": 2, "start": 5, "end": 6},
                          {"index": 3, "start": 6, "end": 10}])

    def test_baseline_first_failure_is_dual_start_segment(self):
        summary = self.audit["summary"]
        self.assertFalse(summary["passed"])
        first = summary["firstFailure"]
        self.assertEqual((first["start"], first["end"]), (4, 5))
        self.assertEqual(first["requiredGenerations"], ["old", "new"])

    def test_baseline_shortfall_is_one_new_copy(self):
        rows = {row["index"]: row for row in scenario_rows(self.audit)}
        self.assertFalse(rows[1]["recoverable"])
        self.assertEqual(rows[1]["shortfall"], {"old": 0, "new": 1})
        self.assertEqual(rows[1]["copies"]["old"], ["old-a", "old-b"])
        # Only one usable new copy exists, so the partial witness holds it.
        self.assertEqual(rows[1]["copies"]["new"], ["new-c"])
        self.assertTrue(rows[0]["recoverable"])
        self.assertTrue(rows[3]["recoverable"])

    def test_repair_selects_cheapest_action_that_covers_the_whole_window(self):
        self.assertEqual(self.repair["status"], "feasible")
        # action-3 is cheaper but only covers [5, 6); the dual window starts at
        # 4, so the tie between action-1 and action-2 is broken by the ID.
        self.assertEqual(self.repair["selected"], ["action-1"])
        self.assertEqual(self.repair["cost"], 10)
        self.assertEqual(self.repair["actionCount"], 1)
        self.assertTrue(self.repair["summary"]["passed"])

    def test_repair_report_matches_audit_of_the_repaired_configuration(self):
        rows = scenario_rows(self.repair)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1]["copies"]["new"], ["new-c", "new-d-1"])
        self.assertTrue(all(row["recoverable"] for row in rows))

    def test_both_commands_share_the_segmentation(self):
        self.assertEqual(self.audit["case"]["scenarioDigest"],
                         self.repair["case"]["scenarioDigest"])
        self.assertEqual(self.audit["segments"], self.repair["segments"])


# ---------------------------------------------------------------------------
# Failure-domain structure
# ---------------------------------------------------------------------------


class SharedFailureDomainTest(unittest.TestCase):
    """Two sites whose copies must not rely on the same physical room."""

    def test_shared_domain_breaks_the_dual_witness(self):
        document = make_case(
            domains=("site-a", "site-b"),
            budget=1,
            copies=[
                copy("old-a", "old", "o1", "A", ("site-a",)),
                copy("old-b", "old", "o2", "B", ("site-b",)),
                copy("new-a", "new", "n1", "N1", ("site-a",)),
                copy("new-c", "new", "n2", "C", ("site-b",)),
            ],
        )
        _, audit, _ = run(document)
        rows = scenario_rows(audit)
        # No failure: fine.  site-a down: only site-b copies survive, so the new
        # generation loses its only first-share copy and the witness fails.
        by_faults = {tuple(row["faults"]): row for row in rows
                     if (row["start"], row["end"]) == (4, 5)}
        self.assertTrue(by_faults[()]["recoverable"])
        # One site down leaves one copy per generation, each short by one.
        self.assertFalse(by_faults[("site-a",)]["recoverable"])
        self.assertEqual(by_faults[("site-a",)]["shortfall"], {"old": 1, "new": 1})
        self.assertFalse(by_faults[("site-b",)]["recoverable"])

    def test_single_domains_pass_but_the_pair_fails(self):
        # Both single-site failures leave a complete witness, yet losing both
        # sites at once kills the only two copies of one new share.  A per-site
        # check (or a per-copy check) would miss this, which is why the drill
        # enumerates domain *combinations*, not just single domains.
        document = make_case(
            domains=("site-a", "site-b"),
            budget=2,
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-1", "new", "n1", "C"),
                copy("new-2a", "new", "n2", "D", ("site-a",)),
                copy("new-2b", "new", "n2", "F", ("site-b",)),
            ],
        )
        _, audit, _ = run(document)
        rows = scenario_rows(audit)
        dual = [row for row in rows if (row["start"], row["end"]) == (4, 5)]
        by_faults = {tuple(row["faults"]): row for row in dual}
        self.assertTrue(by_faults[()]["recoverable"])
        self.assertTrue(by_faults[("site-a",)]["recoverable"])
        self.assertTrue(by_faults[("site-b",)]["recoverable"])
        pair = by_faults[("site-a", "site-b")]
        self.assertFalse(pair["recoverable"])
        self.assertEqual(pair["shortfall"], {"old": 0, "new": 1})
        # The partial witness still keeps both old copies and the surviving
        # new share.
        self.assertEqual(pair["copies"], {"old": ["old-a", "old-b"],
                                          "new": ["new-1"]})


# ---------------------------------------------------------------------------
# Share and custodian separation
# ---------------------------------------------------------------------------


class SeparationTest(unittest.TestCase):
    def test_two_copies_of_one_share_cannot_satisfy_a_threshold(self):
        document = make_case(
            copies=[
                copy("old-a1", "old", "o1", "A"),
                copy("old-a2", "old", "o1", "B"),   # same share, other custodian
                copy("new-a", "new", "n1", "N1"),
                copy("new-c", "new", "n2", "N2"),
            ],
        )
        _, audit, _ = run(document)
        rows = scenario_rows(audit)
        early = [row for row in rows if (row["start"], row["end"]) == (0, 4)][0]
        self.assertFalse(early["recoverable"])
        self.assertEqual(early["shortfall"], {"old": 1})
        # Only one copy of the single available share may be picked.
        self.assertEqual(early["copies"]["old"], ["old-a1"])

    def test_one_person_cannot_serve_both_generations(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "A"),   # same person as old-a
                copy("new-c", "new", "n2", "C"),
            ],
        )
        _, audit, _ = run(document)
        dual = [row for row in scenario_rows(audit)
                if (row["start"], row["end"]) == (4, 5)][0]
        self.assertFalse(dual["recoverable"])
        self.assertEqual(dual["shortfall"], {"old": 0, "new": 1})
        self.assertEqual(dual["copies"], {"old": ["old-a", "old-b"],
                                          "new": ["new-c"]})

    def test_one_person_cannot_hold_two_shares_of_the_same_generation(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "A"),
                copy("new-a", "new", "n1", "A"),
                copy("new-c", "new", "n2", "C"),
            ],
        )
        _, audit, _ = run(document)
        early = [row for row in scenario_rows(audit)
                 if (row["start"], row["end"]) == (0, 4)][0]
        self.assertFalse(early["recoverable"])
        self.assertEqual(early["shortfall"], {"old": 1})
        self.assertEqual(early["copies"]["old"], ["old-a"])


# ---------------------------------------------------------------------------
# Partial-witness tie-breaking
# ---------------------------------------------------------------------------


class PartialWitnessTest(unittest.TestCase):
    def test_lexicographically_smallest_maximal_partial_witness(self):
        # The dual window needs two new copies but only one share is available,
        # so the best total is 3: two old copies plus one new copy.  Several new
        # copies qualify; the smallest ID must win.
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-m", "new", "n1", "M"),
                copy("new-z", "new", "n1", "Z"),
                copy("new-q", "new", "n1", "Q"),
            ],
        )
        _, audit, _ = run(document)
        dual = [row for row in scenario_rows(audit)
                if (row["start"], row["end"]) == (4, 5)][0]
        self.assertEqual(dual["copies"], {"old": ["old-a", "old-b"],
                                          "new": ["new-m"]})
        self.assertEqual(dual["shortfall"], {"old": 0, "new": 1})

    def test_partial_witness_prefers_a_prefix_over_an_extension(self):
        # Two partial witnesses reach the same maximal total of 2:
        #   split (2, 0) -> old ["old-a", "old-b"], new []
        #   split (1, 1) -> old ["old-a"],           new ["new-c"]
        # The new copy shares custodian B with old-b, so the two old copies and
        # the new one cannot be used together.  Nested-list order makes the
        # shorter old list smaller (a prefix sorts before its extensions), so
        # the (1, 1) split must win.
        document = make_case(
            old_threshold=2, new_threshold=1,
            new_shares=("n1",),
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-c", "new", "n1", "B"),
            ],
        )
        _, audit, _ = run(document)
        dual = [row for row in scenario_rows(audit)
                if (row["start"], row["end"]) == (4, 5)][0]
        self.assertEqual(dual["copies"], {"old": ["old-a"],
                                          "new": ["new-c"]})
        self.assertEqual(dual["shortfall"], {"old": 1, "new": 0})

    def test_empty_required_generation_lists_are_kept(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("new-c", "new", "n1", "C"),
            ],
        )
        _, audit, _ = run(document)
        dual = [row for row in scenario_rows(audit)
                if (row["start"], row["end"]) == (5, 6)][0]
        self.assertFalse(dual["recoverable"])
        self.assertEqual(dual["copies"], {"old": ["old-a"], "new": ["new-c"]})
        self.assertEqual(dual["shortfall"], {"old": 1, "new": 1})


# ---------------------------------------------------------------------------
# Time boundaries
# ---------------------------------------------------------------------------


class BoundaryTest(unittest.TestCase):
    def test_adjacent_intervals_behave_as_a_union(self):
        base = [
            copy("old-a", "old", "o1", "A", intervals=((0, 5), (5, 10))),
            copy("old-b", "old", "o2", "B"),
            copy("new-a", "new", "n1", "N1"),
            copy("new-c", "new", "n2", "C"),
        ]
        _, audit, _ = run(make_case(copies=base))
        self.assertTrue(audit["summary"]["passed"])
        # The union does not add a cut point at 5 for this copy alone, but the
        # drill instants already cut there; there are still four segments.
        self.assertEqual(len(audit["segments"]), 4)

    def test_each_interval_endpoint_becomes_a_cut(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A", intervals=((0, 2), (2, 10))),
                copy("old-b", "old", "o2", "B", intervals=((1, 3), (7, 9))),
                copy("new-a", "new", "n1", "A"),
                copy("new-c", "new", "n2", "C"),
            ],
        )
        _, audit, _ = run(document)
        starts = [segment["start"] for segment in audit["segments"]]
        self.assertEqual(starts, [0, 1, 2, 3, 4, 5, 6, 7, 9])

    def test_unavailable_copy_fails_only_its_own_segment(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A", intervals=((0, 4),)),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
                copy("new-c", "new", "n2", "C"),
            ],
        )
        _, audit, _ = run(document)
        rows = {(row["start"], row["end"]): row for row in scenario_rows(audit)}
        self.assertTrue(rows[(0, 4)]["recoverable"])
        self.assertFalse(rows[(4, 5)]["recoverable"])
        self.assertEqual(rows[(4, 5)]["shortfall"], {"old": 1, "new": 0})
        self.assertTrue(rows[(6, 10)]["recoverable"])


# ---------------------------------------------------------------------------
# Repair constraints
# ---------------------------------------------------------------------------


class RepairTest(unittest.TestCase):
    def test_already_passing_baseline_needs_no_action(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
                copy("new-c", "new", "n2", "C"),
            ],
            actions=[action("extra", 5, id="extra-copy", generation="new",
                            share="n2", custodian="D", domains=(), intervals=[[0, 10]])],
        )
        _, audit, repair = run(document)
        self.assertTrue(audit["summary"]["passed"])
        self.assertEqual(repair["status"], "feasible")
        self.assertEqual(repair["selected"], [])
        self.assertEqual(repair["cost"], 0)

    def test_max_actions_prevents_a_two_action_fix(self):
        # The dual window loses one copy in each of two generations, so it
        # needs two actions, but only one is allowed.
        document = make_case(
            max_actions=1,
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B", intervals=((0, 4),)),
                copy("new-a", "new", "n1", "A"),
                copy("new-c", "new", "n2", "C", intervals=((0, 4),)),
            ],
            actions=[
                action("fix-old", 1, id="fix-old-copy", generation="old",
                       share="o2", custodian="B", domains=(), intervals=[[0, 10]]),
                action("fix-new", 1, id="fix-new-copy", generation="new",
                       share="n2", custodian="C", domains=(), intervals=[[0, 10]]),
            ],
        )
        _, audit, repair = run(document)
        self.assertFalse(audit["summary"]["passed"])
        self.assertEqual(repair["status"], "impossible")
        self.assertTrue(repair["reason"])

    def test_zero_action_budget_reports_impossible(self):
        document = make_case(
            max_actions=0,
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B", intervals=((0, 4),)),
                copy("new-a", "new", "n1", "A"),
                copy("new-c", "new", "n2", "C"),
            ],
            actions=[action("fix-old", 1, id="fix-old-copy", generation="old",
                            share="o2", custodian="B", domains=(),
                            intervals=[[0, 10]])],
        )
        _, _, repair = run(document)
        self.assertEqual(repair["status"], "impossible")

    def test_cost_outranks_action_count(self):
        # The new generation loses its second share from 4 on.  Two cheap
        # actions cover [4, 6) and [6, 10) for 2 total; one action covers the
        # whole window for 5.  Cost is minimised first, so the pair wins even
        # though it uses two actions.
        document = make_case(
            drill=(0, 4, 5, 6, 10),
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
                copy("new-b", "new", "n2", "C", intervals=((0, 4),)),
            ],
            actions=[
                action("cheap-early", 1, id="cheap-early-copy", generation="new",
                       share="n2", custodian="P", domains=(), intervals=[[4, 6]]),
                action("cheap-late", 1, id="cheap-late-copy", generation="new",
                       share="n2", custodian="Q", domains=(), intervals=[[6, 10]]),
                action("both", 5, id="both-copy", generation="new",
                       share="n2", custodian="R", domains=(), intervals=[[4, 10]]),
            ],
        )
        _, _, repair = run(document)
        self.assertEqual(repair["selected"], ["cheap-early", "cheap-late"])
        self.assertEqual(repair["cost"], 2)
        self.assertEqual(repair["actionCount"], 2)

    def test_action_count_outranks_ids_at_equal_cost(self):
        # Both options cost 4: one action, or two actions of 2 each.  Fewer
        # actions wins before the ID comparison.
        document = make_case(
            drill=(0, 4, 5, 6, 10),
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
                copy("new-b", "new", "n2", "C", intervals=((0, 4),)),
            ],
            actions=[
                action("aaa-early", 2, id="aaa-early-copy", generation="new",
                       share="n2", custodian="P", domains=(), intervals=[[4, 6]]),
                action("aaa-late", 2, id="aaa-late-copy", generation="new",
                       share="n2", custodian="Q", domains=(), intervals=[[6, 10]]),
                action("zzz-both", 4, id="zzz-both-copy", generation="new",
                       share="n2", custodian="R", domains=(), intervals=[[4, 10]]),
            ],
        )
        _, _, repair = run(document)
        self.assertEqual(repair["selected"], ["zzz-both"])
        self.assertEqual(repair["cost"], 4)
        self.assertEqual(repair["actionCount"], 1)

    def test_action_usable_in_no_failing_scenario_is_never_selected(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
            ],
            actions=[
                action("late", 1, id="late-copy", generation="new",
                       share="n2", custodian="L", domains=(), intervals=[[7, 10]]),
                action("useful", 2, id="useful-copy", generation="new",
                       share="n2", custodian="U", domains=(), intervals=[[4, 10]]),
            ],
        )
        _, _, repair = run(document)
        self.assertEqual(repair["selected"], ["useful"])

    def test_ties_are_broken_by_the_sorted_action_id_list(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
            ],
            actions=[
                action("zulu", 4, id="zulu-copy", generation="new", share="n2",
                       custodian="Z", domains=(), intervals=[[4, 10]]),
                action("alpha", 4, id="alpha-copy", generation="new", share="n2",
                       custodian="Y", domains=(), intervals=[[4, 10]]),
            ],
        )
        _, _, repair = run(document)
        self.assertEqual(repair["selected"], ["alpha"])

    def test_repair_can_need_two_actions(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B", intervals=((0, 4),)),
                copy("new-a", "new", "n1", "N1"),
                copy("new-c", "new", "n2", "C", intervals=((0, 4),)),
            ],
            actions=[
                action("fix-old", 3, id="fix-old-copy", generation="old",
                       share="o2", custodian="B2", domains=(), intervals=[[4, 10]]),
                action("fix-new", 4, id="fix-new-copy", generation="new",
                       share="n2", custodian="C2", domains=(), intervals=[[4, 10]]),
            ],
        )
        _, _, repair = run(document)
        self.assertEqual(repair["selected"], ["fix-new", "fix-old"])
        self.assertEqual(repair["cost"], 7)
        self.assertEqual(repair["actionCount"], 2)
        self.assertTrue(repair["summary"]["passed"])


# ---------------------------------------------------------------------------
# Input-order independence and determinism
# ---------------------------------------------------------------------------


class DeterminismTest(unittest.TestCase):
    def test_shuffled_input_produces_identical_output(self):
        document = sample_document()
        _, audit_a, repair_a = run(document)

        shuffled = copy_module.deepcopy(document)
        random.Random(7).shuffle(shuffled["copies"])
        random.Random(11).shuffle(shuffled["actions"])
        shuffled["domains"] = list(reversed(shuffled["domains"]))
        for entry in shuffled["copies"]:
            random.Random(hash(entry["id"])).shuffle(entry["intervals"])
            random.Random(hash(entry["id"])).shuffle(entry["domains"])
        _, audit_b, repair_b = run(shuffled)
        self.assertEqual(audit_a, audit_b)
        self.assertEqual(repair_a, repair_b)

    def test_repeated_runs_are_identical(self):
        document = sample_document()
        _, audit_1, repair_1 = run(document)
        _, audit_2, repair_2 = run(copy_module.deepcopy(document))
        self.assertEqual(audit_1, audit_2)
        self.assertEqual(repair_1, repair_2)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ValidationTest(unittest.TestCase):
    def assert_rejected(self, document, fragment):
        with self.assertRaises(CaseError) as caught:
            parse(loads(json.dumps(document)))
        self.assertIn(fragment, str(caught.exception))

    def test_duplicate_json_key(self):
        text = '{"drill": {}, "drill": {}}'
        with self.assertRaises(CaseError) as caught:
            loads(text)
        self.assertIn("duplicate JSON key", str(caught.exception))

    def test_duplicate_copy_id(self):
        document = sample_document()
        document["copies"].append(copy("old-a", "old", "o1", "Q"))
        self.assert_rejected(document, "duplicate copy ID")

    def test_duplicate_action_id(self):
        document = sample_document()
        document["actions"][1]["id"] = document["actions"][0]["id"]
        self.assert_rejected(document, "duplicate action ID")

    def test_duplicate_share_within_generation(self):
        document = sample_document()
        document["generations"]["old"]["shares"] = ["o1", "o1"]
        self.assert_rejected(document, "duplicate share ID")

    def test_unknown_share_reference(self):
        document = sample_document()
        document["copies"][0]["share"] = "nope"
        self.assert_rejected(document, "unknown old-generation share")

    def test_share_of_the_other_generation_is_rejected(self):
        document = sample_document()
        document["copies"][0]["share"] = "n1"  # a new-generation share
        self.assert_rejected(document, "unknown old-generation share")

    def test_unknown_domain_reference(self):
        document = sample_document()
        document["copies"][0]["domains"] = ["ghost"]
        self.assert_rejected(document, "unknown fault domain")

    def test_duplicate_domain_on_one_copy(self):
        document = sample_document()
        document["domains"] = ["d1"]
        document["copies"][0]["domains"] = ["d1", "d1"]
        self.assert_rejected(document, "duplicate fault domain")

    def test_boolean_integer_is_rejected(self):
        document = sample_document()
        document["generations"]["old"]["threshold"] = True
        self.assert_rejected(document, "booleans are not integers")

    def test_boolean_budget_is_rejected(self):
        document = sample_document()
        document["budget"] = False
        self.assert_rejected(document, "booleans are not integers")

    def test_float_integer_is_rejected(self):
        document = sample_document()
        document["drill"]["switch"] = 5.0
        self.assert_rejected(document, "expected a JSON integer")

    def test_inverted_interval(self):
        document = sample_document()
        document["copies"][0]["intervals"] = [[6, 4]]
        self.assert_rejected(document, "empty or inverted interval")

    def test_empty_interval(self):
        document = sample_document()
        document["copies"][0]["intervals"] = [[4, 4]]
        self.assert_rejected(document, "empty or inverted interval")

    def test_interval_outside_the_drill(self):
        document = sample_document()
        document["copies"][0]["intervals"] = [[-1, 4]]
        self.assert_rejected(document, "outside the drill window")

    def test_interval_beyond_the_end_of_the_drill(self):
        document = sample_document()
        document["copies"][0]["intervals"] = [[0, 11]]
        self.assert_rejected(document, "outside the drill window")

    def test_missing_availability(self):
        document = sample_document()
        document["copies"][0]["intervals"] = []
        self.assert_rejected(document, "at least one availability interval")

    def test_inverted_drill_window(self):
        document = sample_document()
        document["drill"]["dual_end"] = 4
        self.assert_rejected(document, "start <= dual_start < switch < dual_end <= end")

    def test_switch_outside_the_dual_window(self):
        document = sample_document()
        document["drill"]["switch"] = 4     # equal to dual_start
        self.assert_rejected(document, "start <= dual_start < switch < dual_end <= end")

    def test_budget_out_of_range(self):
        document = sample_document()
        document["budget"] = 3
        self.assert_rejected(document, "outside the supported range")

    def test_max_actions_out_of_range(self):
        document = sample_document()
        document["max_actions"] = 5
        self.assert_rejected(document, "outside the supported range")

    def test_non_positive_threshold(self):
        document = sample_document()
        document["generations"]["new"]["threshold"] = 0
        self.assert_rejected(document, "positive integer")

    def test_non_positive_cost(self):
        document = sample_document()
        document["actions"][0]["cost"] = 0
        self.assert_rejected(document, "positive integer")

    def test_threshold_above_share_count(self):
        document = sample_document()
        document["generations"]["old"]["threshold"] = 3
        self.assert_rejected(document, "exceeds the declared share count")

    def test_candidate_copy_id_collision(self):
        document = sample_document()
        document["actions"][0]["copy"]["id"] = "old-a"
        self.assert_rejected(document, "collides")

    def test_bad_identifier(self):
        document = sample_document()
        document["copies"][0]["custodian"] = "A B"
        self.assert_rejected(document, "must match")

    def test_unknown_field(self):
        document = sample_document()
        document["copies"][0]["colour"] = "blue"
        self.assert_rejected(document, "unknown field")

    def test_missing_top_level_field(self):
        document = sample_document()
        del document["domains"]
        self.assert_rejected(document, "missing field")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CommandLineTest(unittest.TestCase):
    def _run(self, *arguments):
        return subprocess.run([sys.executable, "-m", "rotation_audit", *arguments],
                              cwd=ROOT, capture_output=True, text=True)

    def _write(self, document):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        try:
            json.dump(document, handle)
        finally:
            handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_audit_exit_codes(self):
        failing = self._run("audit", "examples/case_sample.json")
        self.assertEqual(failing.returncode, 1)
        self.assertEqual(json.loads(failing.stdout)["command"], "audit")

        passing = make_case(copies=[
            copy("old-a", "old", "o1", "A"),
            copy("old-b", "old", "o2", "B"),
            copy("new-a", "new", "n1", "N1"),
            copy("new-q", "new", "n2", "Q"),
        ])
        result = self._run("audit", self._write(passing))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["summary"]["passed"])

    def test_repair_exit_codes(self):
        feasible = self._run("repair", "examples/case_sample.json")
        self.assertEqual(feasible.returncode, 0)
        self.assertEqual(json.loads(feasible.stdout)["status"], "feasible")

        impossible = make_case(
            max_actions=0,
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("new-a", "new", "n1", "A"),
            ],
        )
        result = self._run("repair", self._write(impossible))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "impossible")

    def test_bad_usage_and_bad_input_exit_2(self):
        self.assertEqual(self._run("nonsense", "x.json").returncode, 2)
        self.assertEqual(self._run("audit").returncode, 2)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as handle:
            handle.write("{not json")
        self.addCleanup(os.unlink, handle.name)
        result = self._run("audit", handle.name)
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)

    def test_missing_file_exit_2(self):
        self.assertEqual(self._run("audit", "does-not-exist.json").returncode, 2)


# ---------------------------------------------------------------------------
# Engine against the exhaustive oracle
# ---------------------------------------------------------------------------


def _random_document(rng):
    """A small random case: the oracle enumerates every witness and subset."""
    start, end = 0, 8
    dual_start = rng.randint(1, 3)
    switch = rng.randint(dual_start + 1, 4)
    dual_end = rng.randint(switch + 1, 6)
    domains = ["d1", "d2"][:rng.randint(0, 2)]
    budget = rng.randint(0, min(2, len(domains)))

    old_shares = [f"o{i}" for i in range(1, rng.randint(2, 4) + 1)]
    new_shares = [f"n{i}" for i in range(1, rng.randint(2, 4) + 1)]
    old_copies = [copy(f"old-{index}", "old", share, rng.choice("ABC"),
                       tuple(rng.sample(domains, rng.randint(0, len(domains)))),
                       ((0, rng.randint(2, 8)),))
                  for index, share in enumerate(old_shares)]
    new_copies = [copy(f"new-{index}", "new", share, rng.choice("ABC"),
                       tuple(rng.sample(domains, rng.randint(0, len(domains)))),
                       ((rng.randint(0, 6), 8),))
                  for index, share in enumerate(new_shares)]

    pool_old = list(old_shares)
    pool_new = list(new_shares)
    actions = []
    for index in range(rng.randint(0, 3)):
        generation = rng.choice(["old", "new"])
        pool = pool_old if generation == "old" else pool_new
        actions.append(action(
            f"act-{index}", rng.randint(1, 5),
            id=f"cand-{index}", generation=generation,
            share=rng.choice(pool), custodian=rng.choice("QRS"),
            domains=tuple(rng.sample(domains, rng.randint(0, len(domains)))),
            intervals=[[rng.randint(0, 4), rng.randint(5, 8)]]))

    return make_case(
        drill=(start, dual_start, switch, dual_end, end),
        domains=domains, budget=budget,
        max_actions=rng.randint(0, 2),
        old_shares=old_shares, new_shares=new_shares,
        old_threshold=rng.randint(1, len(old_shares)),
        new_threshold=rng.randint(1, len(new_shares)),
        copies=old_copies + new_copies, actions=actions,
    )


def _normalise(rows):
    return [{"start": row["start"], "end": row["end"], "faults": row["faults"],
             "required": row.get("required", row.get("requiredGenerations")),
             "recoverable": row["recoverable"], "copies": row["copies"],
             "shortfall": row["shortfall"]} for row in rows]


class OracleComparisonTest(unittest.TestCase):
    CASES = 160

    def test_audit_matches_the_exhaustive_oracle(self):
        rng = random.Random(20261121)
        checked = 0
        for _ in range(self.CASES):
            document = _random_document(rng)
            try:
                case = parse(loads(json.dumps(document)))
            except CaseError:
                continue
            report = audit_report(case)
            self.assertEqual(_normalise(report["scenarios"]),
                             _normalise(oracle.scenario_rows(case)),
                             f"audit mismatch on {json.dumps(document)}")
            checked += 1
        self.assertGreater(checked, 100)

    def test_repair_matches_the_exhaustive_oracle(self):
        rng = random.Random(424242)
        checked = 0
        for _ in range(self.CASES):
            document = _random_document(rng)
            try:
                case = parse(loads(json.dumps(document)))
            except CaseError:
                continue
            expected = oracle.best_repair(case)
            report = repair_report(case)
            if expected is None:
                self.assertEqual(report["status"], "impossible",
                                 f"repair mismatch on {json.dumps(document)}")
            else:
                self.assertEqual(report["status"], "feasible")
                self.assertEqual(report["selected"], list(expected[2]))
                self.assertEqual(report["cost"], expected[0])
                self.assertEqual(report["actionCount"], expected[1])
                self.assertTrue(report["summary"]["passed"])
                # The full repaired report must match the oracle run with the
                # chosen candidate copies added to the baseline.
                chosen = [case.actions_by_id()[action_id].copy
                          for action_id in expected[2]]
                self.assertEqual(
                    _normalise(report["scenarios"]),
                    _normalise(oracle.scenario_rows(case, chosen)),
                    f"repaired report mismatch on {json.dumps(document)}")
            checked += 1
        self.assertGreater(checked, 100)






class CandidateWindowTest(unittest.TestCase):
    """A selected candidate must only be usable inside its own intervals."""

    def test_candidate_is_not_usable_outside_its_window(self):
        document = make_case(
            copies=[
                copy("old-a", "old", "o1", "A"),
                copy("old-b", "old", "o2", "B"),
                copy("new-a", "new", "n1", "N1"),
                copy("new-c", "new", "n2", "C", intervals=((0, 4),)),
            ],
            actions=[
                action("late-fix", 1, id="late-fix-copy", generation="new",
                       share="n2", custodian="L", domains=(), intervals=[[6, 10]]),
            ],
        )
        case, _, repair = run(document)
        # The gap is [4, 6); the only candidate starts at 6, so it cannot fix
        # the failing window.
        self.assertEqual(repair["status"], "impossible")
        self.assertIn("has no candidate copy", repair["reason"])

    def test_baseline_failure_with_candidate_that_covers_only_part_is_impossible(self):
        document = sample_document()
        document["actions"] = [
            action("only-late", 1, id="only-late-copy", generation="new",
                   share="n1", custodian="D", domains=(), intervals=[[5, 6]]),
        ]
        _, _, repair = run(document)
        self.assertEqual(repair["status"], "impossible")

    def test_repaired_report_never_uses_a_candidate_outside_its_interval(self):
        document = sample_document()
        case, _, repair = run(document)
        space = Space(case)
        by_copy_id = dict(case.copies_by_id())
        for candidate in case.actions:
            by_copy_id[candidate.copy.id] = candidate.copy
        for row, scenario in zip(repair["scenarios"], space.scenarios):
            for generation, ids in row["copies"].items():
                for copy_id in ids:
                    spec = by_copy_id[copy_id]
                    self.assertTrue(spec.covers(scenario.start),
                                    f"{copy_id} used at {scenario.start} outside its interval")


# ---------------------------------------------------------------------------
# Support envelope
# ---------------------------------------------------------------------------


class EnvelopeTest(unittest.TestCase):
    """A case at the documented limits stays fast and stays exact."""

    def test_large_case_runs_quickly(self):
        import time

        rng = random.Random(2026)
        domains = [f"d{index}" for index in range(10)]
        old_shares = [f"o{index:02d}" for index in range(10)]
        new_shares = [f"n{index:02d}" for index in range(10)]
        copies = []
        for index in range(30):
            lo, hi = sorted(rng.sample(range(0, 20), 2))
            copies.append(copy(f"old-{index:02d}", "old", old_shares[index % 10],
                               f"p{index}", tuple(rng.sample(domains, rng.randint(0, 2))),
                               ((lo, hi),)))
        for index in range(30):
            lo, hi = sorted(rng.sample(range(0, 20), 2))
            copies.append(copy(f"new-{index:02d}", "new", new_shares[index % 10],
                               f"q{index}", tuple(rng.sample(domains, rng.randint(0, 2))),
                               ((lo, hi),)))
        actions = [action(f"a{index:02d}", index + 1, id=f"cand-{index:02d}",
                          generation="old" if index % 2 else "new",
                          share=(old_shares if index % 2 else new_shares)[index % 10],
                          custodian=f"r{index}",
                          domains=tuple(rng.sample(domains, rng.randint(0, 1))),
                          intervals=[[0, 20]])
                   for index in range(16)]
        document = make_case(drill=(0, 6, 10, 14, 20), domains=domains, budget=2,
                             max_actions=4, old_shares=old_shares,
                             new_shares=new_shares, old_threshold=6,
                             new_threshold=6, copies=copies, actions=actions)
        case = parse(loads(json.dumps(document)))
        started = time.perf_counter()
        audit = audit_report(case)
        repair = repair_report(case)
        elapsed = time.perf_counter() - started
        self.assertEqual(audit["summary"]["scenarioCount"], len(audit["scenarios"]))
        self.assertIn(repair["status"], ("feasible", "impossible"))
        self.assertLess(elapsed, 30.0)

    def test_too_many_segments_is_rejected(self):
        # 40 staggered endpoints inside a long drill force more than 24 cuts.
        copies = [copy(f"c{index:02d}", "old", "o1", f"p{index}",
                       intervals=((index, 60),)) for index in range(40)]
        document = make_case(drill=(0, 10, 20, 30, 60), old_shares=("o1",),
                             old_threshold=1, copies=copies, actions=[])
        with self.assertRaises(CaseError) as caught:
            parse(loads(json.dumps(document)))
        self.assertIn("segments", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
