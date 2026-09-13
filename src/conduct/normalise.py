"""The only conversion from SDK evidence to the assertion model."""

from opentelemetry.sdk.trace import ReadableSpan

from .model import Event, Span, SpanKind, SpanStatus, freeze_attributes


def normalise_span(span: ReadableSpan) -> Span:
    context = span.context
    if context is None or span.start_time is None or span.end_time is None:
        raise ValueError("Only completed spans with an identity can be normalised")
    return Span(
        trace_id=f"{context.trace_id:032x}",
        span_id=f"{context.span_id:016x}",
        parent_span_id=f"{span.parent.span_id:016x}" if span.parent else None,
        name=span.name,
        kind=SpanKind(span.kind.name),
        status=SpanStatus(span.status.status_code.name),
        status_description=span.status.description,
        start_time_ns=span.start_time,
        end_time_ns=span.end_time,
        attributes=freeze_attributes(span.attributes or {}),
        events=tuple(
            Event(event.name, event.timestamp, freeze_attributes(event.attributes or {}))
            for event in span.events
        ),
    )
