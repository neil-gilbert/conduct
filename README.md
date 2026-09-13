# Conduct

**Test how your system behaves.**

Conduct lets you test what an application does: whether a declined payment was persisted, a suspended account contacted a provider, or a failed request was retried. Drive the application through its public API and assert its observable behaviour without checking internal method calls.

The evidence comes from the application’s existing OpenTelemetry instrumentation. It works with ordinary Python, `unittest`, pytest, Behave, or another harness, without fixtures, plugins, a collector, or a separate runner.

Conduct is an in-process Python testing library. Its design is documented in [DESIGN.md](DESIGN.md) and the accepted [architecture decisions](docs/adr).

## Install and run

Python 3.11–3.14 is supported. Install from this repository (Conduct has not been published to PyPI):

```sh
python -m pip install "conduct @ git+https://github.com/neil-gilbert/conduct.git"
```

To work on the library and run the examples:

```sh
git clone https://github.com/neil-gilbert/conduct.git
cd conduct
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m examples.payments
python -m unittest discover -s tests -v
```

The runtime depends only on `opentelemetry-api>=1.44,<2` and `opentelemetry-sdk>=1.44,<2` (plus their transitive dependencies). No host test framework is a runtime dependency.

## First observation

Use the **same explicit SDK provider as the application**. The application must already emit the domain spans you want to assert.

```python
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from conduct import Telemetry, expect, have_attribute, occur_once, succeed

provider = TracerProvider(sampler=ALWAYS_ON)
tracer = provider.get_tracer("my-application")

def submit_payment(amount, currency):
    with tracer.start_as_current_span("payment.authorise") as span:
        span.set_attributes({"payment.amount": amount, "payment.currency": currency})
        return {"accepted": True}

try:
    with Telemetry(provider=provider) as telemetry:
        with telemetry.observe() as observation:
            result = submit_payment(1000, "GBP")

        assert result["accepted"]
        expect(observation.spans.named("payment.authorise")).to(
            occur_once(),
            have_attribute("payment.currency", "GBP"),
            succeed(),
        )
finally:
    provider.shutdown()  # Caller-owned; Telemetry never shuts it down.
```

The [payment example](examples/payments.py) and [behavioural tests](tests/test_payments.py) cover declines, suspended accounts, retries, persistence, and diagnostic output, without assertions against internal calls.

## Selection and expectations

Selections match exact names and an attribute subset across **all traces in an observation**. They are immutable, composable queries, not assertions:

```python
requests = (
    observation.spans.named("http.client")
    .where_attribute("server.address", "api.example.com")
    .where_attribute("http.request.method", "POST")
)
expect(requests).to(occur(2))
```

Import each condition from `conduct`:

| Condition | Meaning |
| --- | --- |
| `exist()` | At least one selected span |
| `not_occur()` | Zero selected spans |
| `occur_once()` / `occur(n)` | Exactly one / exactly n selected spans |
| `have_attribute(key, value)` | An attribute with the expected value |
| `succeed()` | Status OK or UNSET (no recorded error) |
| `fail()` | Status ERROR |
| `have_status(SpanStatus.OK)` | An exact normalised status |
| `have_event(name, **attributes)` | One event matching the name and attribute subset |
| `complete_within("500ms")` | Duration at most the threshold, inclusive |
| `be_child_of(name)` / `be_parent_of(name)` | A direct relationship |
| `be_descendant_of(name)` / `be_ancestor_of(name)` | A transitive relationship |
| `have_children(*names)` | Unordered subset of direct children |
| `have_exact_children(*names)` | Complete unordered set of direct children |
| `occur_in_order(*names)` | An ordered subsequence of selected spans in one trace |

Per-span conditions require `occur_once()` (or `occur(1)`) in the same expectation, or an explicit quantifier:

```python
from conduct import any_span, every_span, expect, have_attribute, occur, succeed

expect(requests).to(occur(2), every_span(succeed()))
expect(requests).to(any_span(have_attribute("retry.attempt", 1), succeed()))
```

`any_span(a, b)` requires one span to satisfy **both** conditions. `every_span(...)` requires every candidate to satisfy all conditions. Both fail on empty selections. Bare `expect(selection).to(succeed())` raises `ExpectationUsageError`, even if there happens to be one candidate. Several shorthand snippets in the draft design omit this cardinality rule; the implementation follows the design's explicit rule.

All conditions are evaluated together; a `TraceAssertionError` includes candidate counts, differences, trace/span IDs, and a bounded trace tree. `any_span` reports the closest candidate by fewest condition differences, breaking ties by start time and IDs. Extra spans, attributes, and events are accepted by default.

Attribute equality is type-sensitive: `True`, `1`, `1.0`, and `"1"` differ. Lists and tuples normalise to immutable tuples and compare element by element. No pattern matching or string coercion is implicit. For event keys containing dots, use `have_event("event", **{"domain.key": "value"})`.

Relationships never follow parents or links across traces. Repeated names in child conditions require distinct children. Temporal ordering uses **strictly increasing start timestamps**, allows intervening spans and overlapping durations, and never combines separate traces. Equal timestamps do not establish order.

Durations accept `ns`, `us`/`µs`, `ms`, `s`, `m`, or `datetime.timedelta`. Hard wall-clock thresholds can make CI tests flaky; prefer generous budgets and use deterministic timestamps when testing duration logic.

## Lifecycle and isolation

- An observation begins on context entry and closes on exit; `close()` is also available. Both observations and sessions close idempotently. An observation cannot be re-entered.
- All synchronous and explicitly awaited work must finish before scope exit. The library never waits for detached background work or clears evidence to infer ownership.
- Selections can be constructed inside a scope, but expectations, iteration, length, and `observation.traces` require a closed, valid observation. Each access through a saved selection rechecks validity.
- Correlated open spans at scope exit immediately raise `ObservationIncompleteError` with their names and IDs. The observation remains invalid even if those spans subsequently end.
- Spans started in copied correlation context after completion are quarantined. They emit `LateSpanWarning` and make subsequent access raise `ObservationInvalidError`. Already returned immutable data cannot be revoked, and assertions completed before late work cannot retroactively fail.
- A session refuses to close with active observations. Once none remain, it checks unresolved violations, unregisters, and becomes unusable, even when it raises `TelemetryLifecycleError`.
- A violation is considered reported when an observation API raises its typed error. Merely emitting a warning does not acknowledge it. Session closure aggregates any still-unreported violations; a reported observation stays invalid. This avoids reporting the same failure twice on ordinary nested context-manager exit.
- Processor callbacks never raise into application code. Even with `warnings.simplefilter("error", LateSpanWarning)`, the violation is retained for observation access or session closure. If application code and scope closure both fail, a Python exception group preserves both errors.

One reusable router attaches to each provider. Closing sessions leaves that router inert for ordinary spans, while stale copied contexts can still invalidate their own observation. It neither replaces the global provider nor changes application attributes or existing exporters.

Correlation uses Python `ContextVar` at span start, independent of span end context. Independent tasks or threads can overlap observations. `asyncio.create_task` and `asyncio.to_thread` inherit context; await them before closing. Raw threads require deliberate `contextvars.copy_context().run(...)` propagation to join an observation. Child tasks inheriting an observation cannot nest another one: create independent observations from outside an existing scope. An observation must close in the context that entered it.

V1 is **in-process** and requires the SDK's default synchronous span-processor pipeline; concurrent/custom top-level dispatch is rejected because it may lose the starting context. Existing simple and batch exporters attached to the default pipeline work normally. Cross-process HTTP/message handling requires a future correlation adapter; propagating an OpenTelemetry trace ID alone does not propagate this library's observation context.

## Complete evidence and configuration

This implementation requires `TracerProvider(sampler=ALWAYS_ON)` and rejects parent-based, probabilistic, and custom samplers. This deliberately makes the draft's sampling recommendation a checked prerequisite: a dropped span is invisible to the processor and could otherwise make an absence assertion falsely pass. It is an initial restriction, not an attempt to reconfigure your provider. Keep the provider and its configuration alive and stable for the session.

Providers disabled through `OTEL_SDK_DISABLED` are rejected. Configure span limits large enough for your asserted attributes and events. Detectable dropped attributes/events invalidate the observation. String truncation, attributes rejected by the SDK before recording, individually disabled instrumentation, lost correlation, and spans emitted to a different provider cannot all be detected. The library cannot prove that an application is correctly instrumented; assertions rely on its domain telemetry contract.

The capture adapter uses the SDK's [`SpanProcessor` lifecycle](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html) and `InMemorySpanExporter`, then normalises completed evidence into frozen `Span`, `Event`, `Trace`, and `TraceGraph` objects. To use the SDK-independent query/matcher engine with supplied normalised evidence, use `SpanSelection.from_spans(spans)`.

## Development

```sh
python -m unittest discover -s tests -v
ruff check src tests examples
ruff format --check src tests examples
python -m build
```

CI runs the suite on Python 3.11, 3.12, 3.13, and 3.14 with both the minimum SDK version (1.44.0) and the newest permitted version. Packaging builds a wheel and source distribution. OTLP ingestion, automatic instrumentation, polling, logs, metrics, and framework integrations are outside V1.
