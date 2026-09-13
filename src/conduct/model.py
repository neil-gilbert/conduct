"""Immutable evidence and graph indexes, independent of OpenTelemetry SDK types."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

AttributeScalar: TypeAlias = str | bool | int | float
AttributeValue: TypeAlias = AttributeScalar | tuple[AttributeScalar, ...]


def freeze_value(value: object) -> AttributeValue:
    if type(value) in (str, bool, int, float):
        return value  # type: ignore[return-value]
    if isinstance(value, (tuple, list)) and all(type(v) in (str, bool, int, float) for v in value):
        return tuple(value)
    raise TypeError(f"Unsupported telemetry attribute value: {value!r}")


def freeze_attributes(attributes: Mapping[str, object]) -> Mapping[str, AttributeValue]:
    return MappingProxyType({key: freeze_value(value) for key, value in attributes.items()})


def attribute_equal(actual: AttributeValue, expected: AttributeValue) -> bool:
    """No scalar coercion (including bool/int); list inputs are normalised to tuples."""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, tuple) and isinstance(expected, tuple):
        return len(actual) == len(expected) and all(
            attribute_equal(a, b) for a, b in zip(actual, expected, strict=True)
        )
    return actual == expected


class SpanKind(StrEnum):
    INTERNAL = "INTERNAL"
    SERVER = "SERVER"
    CLIENT = "CLIENT"
    PRODUCER = "PRODUCER"
    CONSUMER = "CONSUMER"


class SpanStatus(StrEnum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Event:
    name: str
    timestamp_ns: int
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    kind: SpanKind
    status: SpanStatus
    start_time_ns: int
    end_time_ns: int
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)
    events: tuple[Event, ...] = ()
    status_description: str | None = None

    def __post_init__(self) -> None:
        if self.end_time_ns < self.start_time_ns:
            raise ValueError("A span cannot end before it starts")
        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))
        object.__setattr__(self, "events", tuple(self.events))

    @property
    def duration_ns(self) -> int:
        return self.end_time_ns - self.start_time_ns


def span_order(span: Span) -> tuple[int, str, str]:
    return span.start_time_ns, span.trace_id, span.span_id


@dataclass(frozen=True)
class Trace:
    trace_id: str
    spans: tuple[Span, ...]
    _by_id: Mapping[str, Span] = field(init=False, repr=False, compare=False)
    _children: Mapping[str | None, tuple[Span, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        spans = tuple(sorted(self.spans, key=span_order))
        by_id = {span.span_id: span for span in spans}
        if len(by_id) != len(spans):
            raise ValueError("Duplicate span identifiers within a trace")
        if any(span.trace_id != self.trace_id for span in spans):
            raise ValueError("Every span in a trace must have the same trace identifier")
        children: dict[str | None, list[Span]] = {}
        for span in spans:
            children.setdefault(span.parent_span_id, []).append(span)
        object.__setattr__(self, "spans", spans)
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))
        object.__setattr__(
            self,
            "_children",
            MappingProxyType({key: tuple(value) for key, value in children.items()}),
        )

    @property
    def roots(self) -> tuple[Span, ...]:
        # A parent may be outside the observation boundary.
        return tuple(s for s in self.spans if s.parent_span_id not in self._by_id)

    def parent(self, span: Span) -> Span | None:
        return self._by_id.get(span.parent_span_id) if span.trace_id == self.trace_id else None

    def children(self, span: Span) -> tuple[Span, ...]:
        return self._children.get(span.span_id, ()) if span.trace_id == self.trace_id else ()

    def ancestors(self, span: Span) -> tuple[Span, ...]:
        result: list[Span] = []
        seen = {span.span_id}
        parent = self.parent(span)
        while parent is not None and parent.span_id not in seen:
            result.append(parent)
            seen.add(parent.span_id)
            parent = self.parent(parent)
        return tuple(result)

    def descendants(self, span: Span) -> tuple[Span, ...]:
        result: list[Span] = []
        seen = {span.span_id}
        pending = list(reversed(self.children(span)))
        while pending:
            child = pending.pop()
            if child.span_id not in seen:
                result.append(child)
                seen.add(child.span_id)
                pending.extend(reversed(self.children(child)))
        return tuple(result)


@dataclass(frozen=True)
class TraceGraph:
    spans: tuple[Span, ...]
    traces: Mapping[str, Trace] = field(init=False)

    def __post_init__(self) -> None:
        spans = tuple(sorted(self.spans, key=span_order))
        groups: dict[str, list[Span]] = {}
        for span in spans:
            groups.setdefault(span.trace_id, []).append(span)
        object.__setattr__(self, "spans", spans)
        object.__setattr__(
            self,
            "traces",
            MappingProxyType({key: Trace(key, tuple(value)) for key, value in groups.items()}),
        )

    @classmethod
    def from_spans(cls, spans: Sequence[Span]) -> "TraceGraph":
        return cls(tuple(spans))
