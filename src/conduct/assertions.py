"""Selection-level conditions and explicit per-span quantifiers."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from .diagnostics import render_failure
from .errors import ExpectationUsageError, TraceAssertionError
from .model import (
    Span,
    SpanStatus,
    Trace,
    TraceGraph,
    attribute_equal,
    freeze_attributes,
    freeze_value,
)
from .query import SpanSelection


class Condition:
    description: str

    def evaluate(self, spans: tuple[Span, ...], graph: TraceGraph) -> tuple[str, ...]:
        raise NotImplementedError


@dataclass(frozen=True)
class SpanCondition(Condition):
    description: str
    _check: Callable[[Span, Trace], tuple[str, ...]]

    def differences(self, span: Span, graph: TraceGraph) -> tuple[str, ...]:
        return self._check(span, graph.traces[span.trace_id])

    def evaluate(self, spans: tuple[Span, ...], graph: TraceGraph) -> tuple[str, ...]:
        return tuple(
            f"{_label(span)}: {difference}"
            for span in spans
            for difference in self.differences(span, graph)
        )


def _label(span: Span) -> str:
    return f"{span.name!r} (trace={span.trace_id}, span={span.span_id})"


@dataclass(frozen=True)
class _Count(Condition):
    expected: int | None

    @property
    def description(self) -> str:
        return (
            "exist (at least one span)"
            if self.expected is None
            else f"occur {self.expected} time(s)"
        )

    def evaluate(self, spans: tuple[Span, ...], graph: TraceGraph) -> tuple[str, ...]:
        matches = bool(spans) if self.expected is None else len(spans) == self.expected
        return () if matches else (f"Expected to {self.description}; found {len(spans)}",)


def exist() -> Condition:
    return _Count(None)


def occur(times: int) -> Condition:
    if type(times) is not int or times < 0:
        raise ExpectationUsageError("Occurrence count must be a non-negative integer")
    return _Count(times)


def occur_once() -> Condition:
    return occur(1)


def not_occur() -> Condition:
    return occur(0)


def have_attribute(key: str, value: object) -> SpanCondition:
    expected = freeze_value(value)

    def check(span: Span, trace: Trace) -> tuple[str, ...]:
        if key not in span.attributes:
            return (f"missing attribute {key!r}; expected {expected!r}",)
        actual = span.attributes[key]
        if attribute_equal(actual, expected):
            return ()
        return (f"attribute {key!r}: expected {expected!r}, observed {actual!r}",)

    return SpanCondition(f"have attribute {key!r} = {expected!r}", check)


def have_status(status: SpanStatus) -> SpanCondition:
    if not isinstance(status, SpanStatus):
        raise ExpectationUsageError("Use conduct.SpanStatus for status expectations")
    return SpanCondition(
        f"have status {status.value}",
        lambda span, trace: (
            ()
            if span.status == status
            else (
                f"expected status {status.value}, observed {span.status.value}"
                + (f" ({span.status_description})" if span.status_description else ""),
            )
        ),
    )


def fail() -> SpanCondition:
    return have_status(SpanStatus.ERROR)


def succeed() -> SpanCondition:
    """Accept OK and UNSET: OpenTelemetry need not explicitly mark successful spans OK."""
    return SpanCondition(
        "succeed (OK or UNSET)",
        lambda span, trace: (
            ()
            if span.status != SpanStatus.ERROR
            else (
                "expected OK or UNSET, observed ERROR"
                + (f" ({span.status_description})" if span.status_description else ""),
            )
        ),
    )


def have_event(name: str, **attributes: object) -> SpanCondition:
    expected = freeze_attributes(attributes)

    def check(span: Span, trace: Trace) -> tuple[str, ...]:
        candidates = [event for event in span.events if event.name == name]
        if not candidates:
            return (f"missing event {name!r}; observed {[event.name for event in span.events]!r}",)
        differences = [
            tuple(
                f"event {name!r} attribute {key!r}: expected {value!r}, "
                + (f"observed {event.attributes[key]!r}" if key in event.attributes else "missing")
                for key, value in expected.items()
                if key not in event.attributes or not attribute_equal(event.attributes[key], value)
            )
            for event in candidates
        ]
        return min(differences, key=len)

    return SpanCondition(f"have event {name!r} with {dict(expected)!r}", check)


def _duration_ns(duration: str | timedelta) -> int:
    if isinstance(duration, timedelta):
        value = (
            (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
        ) * 1000
    elif isinstance(duration, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ns|us|µs|ms|s|m)\s*", duration)
        if not match:
            raise ExpectationUsageError("Use a duration such as '500ms', '2s', or timedelta")
        scale = {"ns": 1, "us": 1000, "µs": 1000, "ms": 1_000_000, "s": 10**9, "m": 60 * 10**9}
        value = int(Decimal(match[1]) * scale[match[2]])
    else:
        raise ExpectationUsageError("Durations require a unit string or timedelta")
    if value < 0:
        raise ExpectationUsageError("Duration must be non-negative")
    return value


def complete_within(duration: str | timedelta) -> SpanCondition:
    limit = _duration_ns(duration)
    return SpanCondition(
        f"complete within {duration}",
        lambda span, trace: (
            ()
            if span.duration_ns <= limit
            else (f"duration {span.duration_ns / 1_000_000:g}ms exceeds {limit / 1_000_000:g}ms",)
        ),
    )


def _names(names: tuple[str, ...]) -> None:
    if not all(isinstance(name, str) for name in names):
        raise ExpectationUsageError("Relationship conditions require exact span-name strings")


def _children(names: tuple[str, ...], exact: bool) -> SpanCondition:
    _names(names)
    expected = Counter(names)

    def check(span: Span, trace: Trace) -> tuple[str, ...]:
        observed = Counter(child.name for child in trace.children(span))
        missing = expected - observed
        extra = observed - expected if exact else Counter()
        if not missing and not extra:
            return ()
        return (
            f"children: missing {list(missing.elements())!r}, "
            f"unexpected {list(extra.elements())!r}; observed {list(observed.elements())!r}",
        )

    return SpanCondition(f"have {'exact ' if exact else ''}children {names!r}", check)


def have_children(*names: str) -> SpanCondition:
    return _children(names, exact=False)


def have_exact_children(*names: str) -> SpanCondition:
    return _children(names, exact=True)


def _relationship(name: str, relation: str) -> SpanCondition:
    _names((name,))

    def check(span: Span, trace: Trace) -> tuple[str, ...]:
        if relation == "child":
            parent = trace.parent(span)
            relatives = (parent,) if parent else ()
        elif relation == "parent":
            relatives = trace.children(span)
        elif relation == "descendant":
            relatives = trace.ancestors(span)
        else:
            relatives = trace.descendants(span)
        return (
            ()
            if any(relative.name == name for relative in relatives)
            else (
                f"expected to be {relation} of {name!r} within this trace; "
                f"observed related spans {[relative.name for relative in relatives]!r}",
            )
        )

    return SpanCondition(f"be {relation} of {name!r}", check)


def be_child_of(name: str) -> SpanCondition:
    return _relationship(name, "child")


def be_parent_of(name: str) -> SpanCondition:
    return _relationship(name, "parent")


def be_descendant_of(name: str) -> SpanCondition:
    return _relationship(name, "descendant")


def be_ancestor_of(name: str) -> SpanCondition:
    return _relationship(name, "ancestor")


@dataclass(frozen=True)
class _Ordered(Condition):
    names: tuple[str, ...]

    @property
    def description(self) -> str:
        return f"occur in start-time order {self.names!r} within one trace"

    def evaluate(self, spans: tuple[Span, ...], graph: TraceGraph) -> tuple[str, ...]:
        for trace_id in {span.trace_id for span in spans}:
            position = 0
            previous_time: int | None = None
            for span in spans:
                if span.trace_id != trace_id:
                    continue
                if span.name == self.names[position] and (
                    previous_time is None or span.start_time_ns > previous_time
                ):
                    position += 1
                    previous_time = span.start_time_ns
                    if position == len(self.names):
                        return ()
        observed = [(span.name, span.start_time_ns, span.trace_id) for span in spans]
        return (
            f"No trace satisfies {self.description}; observed (name, start_ns, trace) {observed!r}",
        )


def occur_in_order(*names: str) -> Condition:
    _names(names)
    if len(names) < 2:
        raise ExpectationUsageError("Temporal order requires at least two span names")
    return _Ordered(names)


@dataclass(frozen=True)
class _Quantifier(Condition):
    mode: str
    conditions: tuple[SpanCondition, ...]

    @property
    def description(self) -> str:
        return f"{self.mode} span: " + "; ".join(c.description for c in self.conditions)

    def evaluate(self, spans: tuple[Span, ...], graph: TraceGraph) -> tuple[str, ...]:
        if not spans:
            return (f"{self.mode}_span requires at least one candidate; found 0",)
        differences = [
            (
                span,
                tuple(
                    d for condition in self.conditions for d in condition.differences(span, graph)
                ),
            )
            for span in spans
        ]
        if self.mode == "any":
            closest, failures = min(differences, key=lambda pair: len(pair[1]))
            return tuple(f"Closest candidate {_label(closest)}: {failure}" for failure in failures)
        return tuple(
            f"{_label(span)}: {failure}" for span, failures in differences for failure in failures
        )


def _quantify(mode: str, conditions: tuple[SpanCondition, ...]) -> Condition:
    if not conditions or not all(isinstance(condition, SpanCondition) for condition in conditions):
        raise ExpectationUsageError("Quantifiers require one or more per-span conditions")
    return _Quantifier(mode, conditions)


def any_span(*conditions: SpanCondition) -> Condition:
    """At least one single span must satisfy all supplied conditions together."""
    return _quantify("any", conditions)


def every_span(*conditions: SpanCondition) -> Condition:
    """Every candidate must satisfy all conditions; an empty selection fails."""
    return _quantify("every", conditions)


@dataclass(frozen=True)
class Expectation:
    selection: SpanSelection

    def to(self, *conditions: Condition) -> None:
        if not conditions or not all(isinstance(condition, Condition) for condition in conditions):
            raise ExpectationUsageError("to(...) requires one or more conditions")
        has_per_span = any(isinstance(condition, SpanCondition) for condition in conditions)
        singular = any(
            isinstance(condition, _Count) and condition.expected == 1 for condition in conditions
        )
        if has_per_span and not singular:
            raise ExpectationUsageError(
                "Per-span conditions require occur_once() or an explicit "
                "any_span(...) / every_span(...), "
                "even if the selection currently contains one span"
            )
        graph, spans = self.selection.resolve()
        differences = tuple(d for condition in conditions for d in condition.evaluate(spans, graph))
        # A copied context may have invalidated the evidence during evaluation.
        self.selection.resolve()
        if differences:
            raise TraceAssertionError(
                render_failure(
                    self.selection.description,
                    tuple(c.description for c in conditions),
                    spans,
                    graph,
                    differences,
                )
            )


def expect(selection: SpanSelection) -> Expectation:
    if not isinstance(selection, SpanSelection):
        raise ExpectationUsageError("expect(...) requires a SpanSelection")
    return Expectation(selection)
