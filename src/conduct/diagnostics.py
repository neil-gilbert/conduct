"""Compact, deterministic evidence suitable for any Python assertion reporter."""

from .model import Span, TraceGraph


def render_failure(
    selection: str,
    conditions: tuple[str, ...],
    candidates: tuple[Span, ...],
    graph: TraceGraph,
    differences: tuple[str, ...],
) -> str:
    lines = [f"Expected {selection} to:"]
    lines.extend(f"  - {condition}" for condition in conditions)
    lines.append(
        f"Found {len(candidates)} candidate span(s) out of {len(graph.spans)} observed span(s)."
    )
    lines.append("Differences:")
    lines.extend(f"  - {difference}" for difference in differences[:30])
    if len(differences) > 30:
        lines.append(f"  ... {len(differences) - 30} more differences")
    trace_ids = {span.trace_id for span in candidates} or set(graph.traces)
    marked = {(span.trace_id, span.span_id) for span in candidates}
    if not graph.spans:
        lines.append(
            "No completed spans were observed. Check the provider and observation boundary."
        )
    remaining = 80
    for trace in graph.traces.values():
        if trace.trace_id not in trace_ids:
            continue
        if remaining <= 0:
            lines.append("  ... additional trace evidence omitted")
            break
        lines.append(f"Observed trace {trace.trace_id}:")
        seen: set[str] = set()
        visible = trace.spans
        trace_candidates = [span for span in candidates if span.trace_id == trace.trace_id]
        if len(visible) > remaining and trace_candidates:
            # Keep failing evidence visible even when it occurs late in a large trace.
            # A few nearby ancestors/children give context without printing thousands of spans.
            focused: set[str] = set()
            for candidate in trace_candidates[:8]:
                focused.add(candidate.span_id)
                focused.update(span.span_id for span in trace.ancestors(candidate)[:3])
                focused.update(span.span_id for span in trace.children(candidate)[:4])
            visible = tuple(span for span in trace.spans if span.span_id in focused)
        visible_ids = {span.span_id for span in visible}
        roots = tuple(span for span in visible if span.parent_span_id not in visible_ids)
        # The fallback also renders malformed/cyclic input supplied to the SDK-independent API.
        for root in (*roots, *visible):
            pending = [(root, 0)]
            while pending and remaining > 0:
                span, depth = pending.pop()
                if span.span_id in seen:
                    continue
                seen.add(span.span_id)
                remaining -= 1
                indent = "  " + "|  " * min(depth, 12)
                marker = " <-- candidate" if (span.trace_id, span.span_id) in marked else ""
                lines.append(
                    f"{indent}{span.name} [{span.status.value}, "
                    f"{span.duration_ns / 1_000_000:g}ms, "
                    f"span={span.span_id}]{marker}"
                )
                if marker:
                    for key, value in list(sorted(span.attributes.items()))[:6]:
                        rendered = repr(value)
                        if len(rendered) > 120:
                            rendered = rendered[:117] + "..."
                        lines.append(f"{indent}  {key} = {rendered}")
                pending.extend(
                    (child, depth + 1)
                    for child in reversed(trace.children(span))
                    if child.span_id in visible_ids
                )
        if len(seen) < len(trace.spans):
            lines.append(f"  ... {len(trace.spans) - len(seen)} more spans in this trace")
    return "\n".join(lines)
