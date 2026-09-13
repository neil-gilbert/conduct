"""Selections describe evidence, without asserting or choosing an arbitrary span."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from .model import AttributeValue, Span, TraceGraph, attribute_equal, freeze_value


@dataclass(frozen=True)
class SpanSelection:
    _snapshot: Callable[[], TraceGraph]
    _names: tuple[str, ...] = ()
    _attributes: tuple[tuple[str, AttributeValue], ...] = ()

    @classmethod
    def from_spans(cls, spans: Sequence[Span]) -> "SpanSelection":
        """Use the SDK-independent assertion engine with already normalised evidence."""
        graph = TraceGraph.from_spans(spans)
        return cls(lambda: graph)

    def named(self, name: str) -> "SpanSelection":
        if not isinstance(name, str):
            raise TypeError("Span names must be strings (matched exactly)")
        return SpanSelection(self._snapshot, (*self._names, name), self._attributes)

    def where_attribute(self, key: str, value: object) -> "SpanSelection":
        return SpanSelection(
            self._snapshot, self._names, (*self._attributes, (key, freeze_value(value)))
        )

    @property
    def description(self) -> str:
        parts = [f"named {name!r}" for name in self._names]
        parts.extend(f"with {key} = {value!r}" for key, value in self._attributes)
        return "spans " + (" and ".join(parts) if parts else "in the observation")

    def resolve(self) -> tuple[TraceGraph, tuple[Span, ...]]:
        graph = self._snapshot()
        return graph, tuple(
            span
            for span in graph.spans
            if all(span.name == name for name in self._names)
            and all(
                key in span.attributes and attribute_equal(span.attributes[key], value)
                for key, value in self._attributes
            )
        )

    def __iter__(self) -> Iterator[Span]:
        return iter(self.resolve()[1])

    def __len__(self) -> int:
        return len(self.resolve()[1])
