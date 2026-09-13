from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from ..model import TraceGraph
from ..normalise import normalise_span


class InMemorySource:
    """One bounded exporter per observation, used synchronously under the router lock."""

    def __init__(self) -> None:
        self._exporter = InMemorySpanExporter()

    def append(self, span: ReadableSpan) -> None:
        self._exporter.export((span,))

    def snapshot(self) -> TraceGraph:
        return TraceGraph(tuple(map(normalise_span, self._exporter.get_finished_spans())))

    def close(self) -> None:
        self._exporter.clear()
        self._exporter.shutdown()
