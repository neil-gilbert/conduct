import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

from conduct import (
    Event,
    ExpectationUsageError,
    Span,
    SpanKind,
    SpanSelection,
    SpanStatus,
    TraceAssertionError,
    TraceGraph,
    any_span,
    be_ancestor_of,
    be_child_of,
    be_descendant_of,
    be_parent_of,
    complete_within,
    every_span,
    exist,
    expect,
    fail,
    have_attribute,
    have_children,
    have_event,
    have_exact_children,
    have_status,
    not_occur,
    occur,
    occur_in_order,
    occur_once,
    succeed,
)


def span(name="operation", sid="1", parent=None, trace="trace", start=0, **kwargs):
    return Span(
        trace,
        sid,
        parent,
        name,
        SpanKind.INTERNAL,
        SpanStatus.UNSET,
        start,
        start + 500_000_000,
        **kwargs,
    )


class AssertionTests(unittest.TestCase):
    def test_presence_absence_and_counts(self):
        selection = SpanSelection.from_spans([span(), span(sid="2")])
        expect(selection).to(exist(), occur(2))
        expect(selection.named("missing")).to(not_occur())
        for condition in (occur_once(), not_occur(), occur(3)):
            with self.subTest(condition=condition), self.assertRaises(TraceAssertionError):
                expect(selection).to(condition)
        with self.assertRaises(TraceAssertionError):
            expect(selection.named("missing")).to(exist())

    def test_selection_is_exact_composable_and_immutable(self):
        selection = SpanSelection.from_spans(
            [
                span("http.client", "1", attributes={"method": "POST", "host": "example.com"}),
                span("httpXclient", "2", attributes={"method": "POST"}),
                span("http.client", "3", attributes={"method": "GET"}),
            ]
        )
        filtered = selection.named("http.client").where_attribute("method", "POST")
        expect(filtered.where_attribute("host", "example.com")).to(occur_once())
        self.assertEqual(len(selection), 3)
        self.assertEqual(len(selection.named("http.*")), 0)
        self.assertEqual(len(filtered.named("other")), 0)

    def test_attribute_values_have_no_scalar_coercion(self):
        selection = SpanSelection.from_spans([span(attributes={"n": 1, "tags": ("a", "b")})])
        expect(selection).to(occur_once(), have_attribute("tags", ["a", "b"]))
        for value in (True, 1.0, "1"):
            with self.subTest(value=value):
                self.assertEqual(len(selection.where_attribute("n", value)), 0)
                with self.assertRaises(TraceAssertionError):
                    expect(selection).to(occur_once(), have_attribute("n", value))

    def test_conditions_do_not_implicitly_quantify_even_one_or_zero_candidates(self):
        for spans in ([], [span()], [span(), span(sid="2")]):
            with (
                self.subTest(spans=spans),
                self.assertRaisesRegex(ExpectationUsageError, "occur_once"),
            ):
                expect(SpanSelection.from_spans(spans)).to(exist(), succeed())

    def test_reports_all_condition_differences_and_cardinality_together(self):
        selection = SpanSelection.from_spans([span(attributes={"currency": "EUR"}), span(sid="2")])
        with self.assertRaises(TraceAssertionError) as raised:
            expect(selection).to(occur_once(), have_attribute("currency", "GBP"), fail())
        message = str(raised.exception)
        for text in (
            "Found 2 candidate",
            "EUR",
            "GBP",
            "missing attribute",
            "observed UNSET",
            "trace",
        ):
            self.assertIn(text, message)

    def test_quantified_conditions_must_match_the_same_span(self):
        selection = SpanSelection.from_spans(
            [span(attributes={"a": 1, "b": 0}), span(sid="2", attributes={"a": 0, "b": 1})]
        )
        expect(selection).to(any_span(have_attribute("a", 1)), every_span(succeed()))
        with self.assertRaisesRegex(TraceAssertionError, "Closest candidate"):
            expect(selection).to(any_span(have_attribute("a", 1), have_attribute("b", 1)))
        with self.assertRaises(TraceAssertionError):
            expect(selection).to(every_span(have_attribute("a", 1)))

    def test_quantifiers_reject_empty_evidence(self):
        for quantified in (any_span(succeed()), every_span(succeed())):
            with self.subTest(quantified=quantified), self.assertRaises(TraceAssertionError):
                expect(SpanSelection.from_spans([])).to(quantified)

    def test_any_span_ranks_closest_by_number_of_differences(self):
        selection = SpanSelection.from_spans(
            [
                span("far", attributes={"x": 0, "y": 0}),
                span("near", sid="2", attributes={"x": 1, "y": 0}),
            ]
        )
        with self.assertRaisesRegex(TraceAssertionError, "Closest candidate 'near'"):
            expect(selection).to(any_span(have_attribute("x", 1), have_attribute("y", 1)))

    def test_status_semantics_and_description(self):
        for status in (SpanStatus.OK, SpanStatus.UNSET):
            expect(SpanSelection.from_spans([replace(span(), status=status)])).to(
                occur_once(), succeed(), have_status(status)
            )
        failed = SpanSelection.from_spans(
            [replace(span(), status=SpanStatus.ERROR, status_description="declined")]
        )
        expect(failed).to(occur_once(), fail())
        with self.assertRaisesRegex(TraceAssertionError, "declined"):
            expect(failed).to(occur_once(), succeed())

    def test_event_attributes_are_a_subset_on_a_single_event(self):
        selection = SpanSelection.from_spans(
            [
                span(
                    events=(
                        Event("declined", 1, {"reason": "funds", "code": 3}),
                        Event("declined", 2, {"reason": "expired", "code": 4}),
                    )
                )
            ]
        )
        expect(selection).to(occur_once(), have_event("declined", reason="funds"))
        with self.assertRaisesRegex(TraceAssertionError, "event 'declined' attribute"):
            expect(selection).to(occur_once(), have_event("declined", reason="funds", code=4))
        with self.assertRaisesRegex(TraceAssertionError, "missing event"):
            expect(selection).to(occur_once(), have_event("accepted"))

    def test_duration_threshold_is_inclusive_and_unit_aware(self):
        selection = SpanSelection.from_spans([span()])
        for duration in ("500ms", "0.5s", "500000us", "500000000ns", timedelta(milliseconds=500)):
            expect(selection).to(occur_once(), complete_within(duration))
        with self.assertRaisesRegex(TraceAssertionError, "500ms exceeds 499ms"):
            expect(selection).to(occur_once(), complete_within("499ms"))

    def test_usage_errors_are_not_assertion_failures(self):
        for factory in (
            lambda: occur(-1),
            lambda: occur(True),
            lambda: occur(1.1),
            lambda: any_span(),
            lambda: every_span(occur_once()),
            lambda: occur_in_order("one"),
            lambda: complete_within("-1s"),
            lambda: complete_within(2),
            lambda: complete_within("nan"),
            lambda: complete_within(timedelta(seconds=-1)),
            lambda: expect([]),
            lambda: expect(SpanSelection.from_spans([])).to(),
            lambda: have_status("OK"),
        ):
            with self.subTest(factory=factory), self.assertRaises(ExpectationUsageError):
                factory()

    def test_empty_failure_includes_capture_guidance(self):
        with self.assertRaisesRegex(TraceAssertionError, "No completed spans"):
            expect(SpanSelection.from_spans([]).named("missing")).to(exist())


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.selection = SpanSelection.from_spans(
            [
                span("root", "1"),
                span("child", "2", "1", start=1),
                span("grandchild", "3", "2", start=2),
                span("extra", "4", "1", start=3),
            ]
        )

    def test_parent_child_and_transitive_relationships(self):
        expect(self.selection.named("root")).to(
            occur_once(),
            have_children("child"),
            have_exact_children("extra", "child"),
            be_parent_of("child"),
            be_ancestor_of("grandchild"),
        )
        expect(self.selection.named("grandchild")).to(
            occur_once(), be_child_of("child"), be_descendant_of("root")
        )
        for condition in (be_child_of("root"), be_descendant_of("missing")):
            with self.subTest(condition=condition), self.assertRaises(TraceAssertionError):
                expect(self.selection.named("grandchild")).to(occur_once(), condition)

    def test_exact_children_are_unordered_and_multiplicity_sensitive(self):
        root = self.selection.named("root")
        for condition in (have_exact_children("child"), have_children("child", "child")):
            with self.subTest(condition=condition), self.assertRaises(TraceAssertionError):
                expect(root).to(occur_once(), condition)
        expect(self.selection.named("grandchild")).to(occur_once(), have_exact_children())

    def test_relationships_never_cross_traces_with_colliding_span_ids(self):
        selection = SpanSelection.from_spans(
            [span("parent", "1", trace="a"), span("child", "2", "1", trace="b", start=1)]
        )
        expect(selection).to(occur(2))
        for subset, condition in (
            ("child", be_child_of("parent")),
            ("child", be_descendant_of("parent")),
            ("parent", be_parent_of("child")),
            ("parent", be_ancestor_of("child")),
            ("parent", have_children("child")),
        ):
            with self.subTest(condition=condition), self.assertRaises(TraceAssertionError):
                expect(selection.named(subset)).to(occur_once(), condition)
        with self.assertRaises(TraceAssertionError):
            expect(selection).to(occur_in_order("parent", "child"))

    def test_order_uses_strict_start_times_and_allows_extra_overlapping_spans(self):
        expect(self.selection).to(occur_in_order("root", "grandchild", "extra"))
        with self.assertRaises(TraceAssertionError):
            expect(self.selection).to(occur_in_order("extra", "root"))
        tied = SpanSelection.from_spans([span("first"), span("second", sid="2")])
        with self.assertRaises(TraceAssertionError):
            expect(tied).to(occur_in_order("first", "second"))

    def test_order_handles_retries_and_selection_filters(self):
        selection = SpanSelection.from_spans(
            [span("retry", "1", start=1), span("retry", "2", start=2), span("done", "3", start=3)]
        )
        expect(selection).to(occur_in_order("retry", "retry", "done"))
        with self.assertRaises(TraceAssertionError):
            expect(selection.named("retry")).to(occur_in_order("retry", "done"))

    def test_model_defensively_freezes_collections(self):
        attributes = {"tags": ["a"]}
        events = [Event("event", 1, attributes)]
        item = span(attributes=attributes, events=events)
        graph = TraceGraph.from_spans([item])
        attributes["tags"].append("b")
        events.clear()
        self.assertEqual(item.attributes["tags"], ("a",))
        self.assertEqual(item.events[0].attributes["tags"], ("a",))
        with self.assertRaises(TypeError):
            item.attributes["new"] = 1
        with self.assertRaises(FrozenInstanceError):
            item.name = "changed"
        with self.assertRaises(TypeError):
            graph.traces["other"] = graph.traces["trace"]

    def test_duplicate_ids_are_rejected_but_missing_parents_are_roots(self):
        with self.assertRaises(ValueError):
            TraceGraph.from_spans([span(), span()])
        item = span(parent="external")
        graph = TraceGraph.from_spans([item])
        self.assertEqual(graph.traces["trace"].roots, (item,))

    def test_cycles_and_deep_traces_do_not_break_diagnostics(self):
        selection = SpanSelection.from_spans([span("a", "1", "2"), span("b", "2", "1")])
        with self.assertRaisesRegex(TraceAssertionError, "Observed trace"):
            expect(selection).to(not_occur())
        items = [span(sid=str(i), parent=str(i - 1), start=i) for i in range(1100)]
        selection = SpanSelection.from_spans(items)
        with self.assertRaisesRegex(TraceAssertionError, "more spans"):
            expect(selection).to(not_occur())

    def test_large_trace_diagnostics_keep_the_relevant_candidate_visible(self):
        spans = [span(sid=str(i), parent=str(i - 1), start=i) for i in range(100)]
        spans.append(span("payment", "100", "99", start=100, attributes={"currency": "EUR"}))
        with self.assertRaises(TraceAssertionError) as raised:
            expect(SpanSelection.from_spans(spans).named("payment")).to(
                occur_once(), have_attribute("currency", "GBP")
            )
        self.assertIn("span=100] <-- candidate", str(raised.exception))
        self.assertIn("currency = 'EUR'", str(raised.exception))
        self.assertIn("more spans", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
