# Conduct: Behavioural Testing for Python

Status: Draft for discussion

Package name: `conduct`

## Summary

Build a small, framework-agnostic Python Testing Library that lets tests drive an application through its public ports and assert its observable behaviour through OpenTelemetry Observations.

An Observation is the bounded telemetry evidence associated with one test action; it may contain more than one OpenTelemetry Trace. The library should turn its completed Spans into an idiomatic assertion API. It should let a test express that a behaviour occurred, did not occur, happened a particular number of times, carried meaningful attributes, or participated in an expected Trace structure—without coupling the test to internal classes, methods, mocks, call graphs, or a particular Host Test Framework.

The initial version should run in-process using OpenTelemetry's `InMemorySpanExporter`. It should not require a collector, server, database, UI, YAML, or separate test runner.

> Drive the system through its ports. Assert its behaviour through telemetry.

## Problem

Integration and component tests often need to prove more than an external response can show. For example:

- a declined payment must not be persisted;
- a suspended account must not cause a payment-provider request;
- a transient provider failure should cause exactly one retry;
- a request must cross a particular architectural boundary;
- a workflow must perform several behaviours in the correct relationship.

These facts are commonly tested with mocks and assertions against implementation details:

```python
payment_gateway.authorise.assert_called_once()
payment_repository.save.assert_not_called()
```

Such tests know about internal object boundaries. Refactoring the implementation can therefore break the tests even when the application's observable behaviour is unchanged.

An instrumented application already emits a machine-readable description of what it did. OpenTelemetry spans contain operation names, attributes, events, status, timing, and parent-child relationships. This project uses that telemetry as the assertion surface:

```python
def test_declined_payment(client, observation):
    response = client.post("/payments", json={"amount": 1000})

    assert response.status_code == 402
    expect(observation.spans.named("payment.gateway.authorise")).to(
        occur_once(),
        fail(),
    )
    expect(observation.spans.named("payment.persist")).to(not_occur())
```

The test remains concerned with behaviour rather than the classes that produced it.

## Product thesis

OpenTelemetry can act as a stable behavioural interface between a running system and its tests.

The opportunity is not another tool for checking whether instrumentation exists. The useful product is a code-first testing library in which traces are:

1. the source of behavioural evidence;
2. a queryable graph rather than a flat list of spans; and
3. rich diagnostic output when an assertion fails.

This fits ports-and-adapters testing particularly well:

```text
test
    |
    | HTTP / CLI / message / function call
    v
driving port
    |
    v
application
    |
    | OpenTelemetry spans
    v
in-memory trace store
    |
    v
behavioural assertions and failure diagnostics
```

The test drives a public entry point and observes the system through a standard telemetry boundary. It does not need to know whether the application uses a `PaymentService`, `AdyenClient`, repository object, or some later replacement.

## Goals

### V1 goals

- Solve mock-coupled behavioural assertions in in-process Python component tests.
- Provide a natural, framework-agnostic Python API over completed OpenTelemetry Spans.
- Capture spans from an in-process Python application with minimal setup.
- Query spans by name and attributes.
- Assert presence, absence, count, status, attributes, events, and duration.
- Assert parent, child, ancestor, and descendant relationships.
- Produce excellent failure messages showing expected and observed behaviour.
- Ignore unrelated spans by default so tests do not become brittle.
- Remain independent of test discovery, fixture, and runner frameworks.
- Keep the core model independent of OpenTelemetry SDK object types where practical.

### Longer-term goals

- Accept OTLP from an out-of-process or containerised application.
- Test applications implemented in languages other than Python.
- Assert distributed workflows across service boundaries.
- Reuse the trace model and query engine in interactive inspection or visualisation tools.

## Non-goals

V1 will not:

- provide a web UI;
- define tests in YAML;
- ship a trace database or general-purpose observability backend;
- replace normal response and domain assertions;
- validate every emitted span or enforce an exact trace by default;
- test the correctness or completeness of an application's instrumentation;
- start as a hosted platform;
- implement an OTLP collector or receiver.
- instrument applications on the user's behalf.
- discover tests, manage suites, provide fixtures, or implement a test runner.
- require or privilege pytest, `unittest`, Behave, or another Host Test Framework.

## Design principles

### Assert intent, tolerate implementation detail

Matchers should describe only the behaviour the test cares about. Extra spans, attributes, and events should be accepted unless strict matching is explicitly requested.

Exact matching is an explicit mode for instrumentation-contract tests, not the default for behavioural tests.

### Treat traces as graphs

A flat span list is enough for basic assertions but not for workflows. Parent-child and ancestor-descendant relationships are first-class concepts in the model and API.

### Make negative behaviour easy to prove

The ability to say that an operation did not happen is a primary feature, not an edge case.

### Prefer domain span names

Tests should assert Domain Telemetry: deliberately chosen semantic operations such as `payment.authorise` that form a Behavioural Contract. Incidental Telemetry, such as a particular HTTP client's internal spans, may provide diagnostic context but should not normally appear in expectations.

### Failures are a product surface

The assertion API is only half the product. A failure should explain what matched, what differed, and where the relevant span sits in the trace.

### Keep collection separate from matching

The same trace model and assertion engine should work first with an in-memory exporter and later with OTLP input. Collection is an adapter, not the core.

## Proposed user experience

### Creating a Telemetry Session

The caller explicitly supplies the same OpenTelemetry SDK provider used by the application. A Telemetry Session is a context manager:

```python
with Telemetry(provider=tracer_provider) as telemetry:
    with telemetry.observe() as observation:
        exercise_system()
```

The library connects capture to that provider but does not discover, replace, or shut down a global provider.

Each provider receives at most one reusable Observation Router. Telemetry Sessions reuse that router rather than installing a new processor each time.

Closing a Telemetry Session:

- rejects closure while any Observation remains active;
- performs a final check for unresolved Lifecycle Violations;
- unregisters the Session from the reusable router;
- makes the Session unusable;
- leaves the supplied provider running and its inert router attached.

An explicit `close()` operation provides the same semantics for callers that cannot use a context manager.

### Basic existence

```python
def test_authorisation_is_traced(client, observation):
    client.post("/payments", json={"amount": 1000})

    expect(observation.spans.named("payment.authorise")).to(exist())
```

### Attributes and status

```python
def test_payment_uses_the_requested_currency(client, observation):
    client.post(
        "/payments",
        json={"amount": 1000, "currency": "GBP"},
    )

    expect(observation.spans.named("payment.authorise")).to(
        occur_once(),
        have_attribute("payment.amount", 1000),
        have_attribute("payment.currency", "GBP"),
        succeed(),
    )
```

### Negative behaviour

```python
def test_suspended_account_does_not_call_provider(client, observation):
    response = client.post(
        "/payments",
        json={"account": "suspended-account", "amount": 1000},
    )

    assert response.status_code == 403
    expect(observation.spans.named("payment.gateway.authorise")).to(not_occur())
```

### Trace relationships

```python
def test_payment_workflow(client, observation):
    client.post("/payments", json={"amount": 1000})

    expect(observation.spans.named("payment.authorise")).to(
        have_children(
            "customer.lookup",
            "payment.gateway.authorise",
            "payment.persist",
        ),
    )

    expect(observation.spans.named("payment.gateway.authorise")).to(
        be_descendant_of("payment.authorise"),
        occur_once(),
    )
```

By default, `have_children(...)` requires the named children to be present within one Trace, allows additional children, and makes no ordering guarantee. Structural completeness and temporal order are separate concepts:

```python
have_children(...)        # unordered subset
have_exact_children(...)  # unordered complete set
occur_in_order(...)       # temporal order, not necessarily parent-child
```

### Events and timing

```python
expect(observation.spans.named("payment.authorise")).to(
    have_event("payment.declined", reason="insufficient_funds"),
)

expect(observation.spans.named("payment.gateway.authorise")).to(
    complete_within("500ms"),
)
```

Timing assertions should be supported, but documentation should warn that hard wall-clock thresholds can make test suites flaky.

### Scoping a single action

Long-lived fixtures or applications may emit unrelated spans. The library needs a clear way to create an Observation that isolates telemetry generated by one action. One possible API is:

```python
def test_payment(client, telemetry):
    with telemetry.observe() as observation:
        client.post("/payments", json={"amount": 1000})

    expect(observation.spans.named("payment.authorise")).to(occur_once())
```

The Observation is a bounded collection of evidence and may contain multiple OpenTelemetry Traces. On leaving the scope, all synchronous and explicitly awaited work must be complete; the Observation reaches its Completion Point and becomes immutable. Detached background work is outside the V1 contract. Isolation details still need to be finalised before the public API is stable.

If any correlated Span remains open at the Completion Point, closing the scope raises `ObservationIncompleteError` with diagnostics identifying the open Spans. The library does not add an implicit grace period.

Expectations may only be evaluated against a closed Observation. This rule applies to positive and negative Expectations alike so every result uses a complete, immutable evidence set.

A correlated Span that starts after the Completion Point is a Late Span. It is quarantined rather than mutating the closed Observation, emits `LateSpanWarning`, and invalidates the Observation. Subsequent access raises `ObservationInvalidError`; unresolved violations are checked again when the Telemetry Session closes. Because detached work is outside V1, an Expectation that completed before a Late Span existed cannot be retroactively failed.

An Observation cannot be nested inside another Observation on the same execution context. Attempting this raises `NestedObservationError`. Independent Observations may overlap in different async tasks or threads when each has its own correlation context.

## Failure experience

Given this assertion:

```python
expect(observation.spans.named("payment.gateway.authorise")).to(
    have_attribute("payment.currency", "GBP"),
)
```

a useful failure might be:

```text
Expected one span named "payment.gateway.authorise" with:
  payment.currency = "GBP"

Found one matching span, but its attribute differed:
  payment.currency = "EUR"

Observed trace 4bf92f3577b34da6a3ce929d0e0e4736:
  payment.authorise
  |-- customer.lookup
  |-- payment.gateway.authorise
  |     payment.currency = "EUR"  <-- mismatch
  `-- payment.persist
```

Failures should include:

- a concise statement of the expectation;
- the number of candidate spans found;
- the closest match when one can be identified;
- attribute, status, event, or relationship differences;
- a compact rendering of the relevant trace subtree;
- trace and span identifiers where useful for deeper investigation.

V1 can raise a purpose-built `TraceAssertionError` with carefully formatted output. Host Test Frameworks can display that exception without bespoke integration.

## Proposed architecture

```text
OpenTelemetry SDK
    |
    v
Span source adapter
    |  V1: InMemorySpanExporter
    |  Later: OTLP receiver, file, backend API
    v
Normaliser
    |
    v
Immutable trace model
    |-- Trace
    |-- Span
    |-- Event
    `-- parent/child index
    |
    v
Query and matcher engine
    |
    +-- Python assertion API
    `-- failure renderer
```

### Core model

The internal model should contain only the data needed for querying and diagnostics:

```python
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
    attributes: Mapping[str, AttributeValue]
    events: tuple[Event, ...]
```

SDK-specific `ReadableSpan` instances should be normalised at the boundary. This prevents OpenTelemetry SDK details from leaking through the matcher API and leaves a path to ingest OTLP data later.

### Span sources

Define a small internal source interface that returns completed spans for a capture boundary. V1 can adapt `InMemorySpanExporter`; future implementations can consume OTLP or stored traces without changing matcher semantics.

### Selection and assertions

`observation.spans.named(name)` creates a Span Selection rather than choosing one arbitrary Span. V1 name selection is exact. Pattern selection, if later justified, will use visibly distinct syntax rather than treating ordinary names as patterns.

`expect(selection).to(...)` evaluates all supplied conditions together and produces one coherent result. Selection, filtering, and expectation construction remain separate concepts.

Conceptually:

```python
selection = (
    observation.spans.named("http.client")
    .where_attribute("server.address", "api.adyen.com")
    .where_attribute("http.request.method", "POST")
)

expect(selection).to(occur_once())
```

This avoids ambiguous behaviour when several Spans have the same name and gives the failure renderer access to all candidates.

All conditions supplied to `to(...)` are evaluated as one Expectation. The library should report all useful differences from the candidate set rather than silently executing assertions partway through a fluent chain.

Per-Span conditions do not silently choose how to handle multiple candidates. The Expectation must establish singular cardinality with `occur_once()` or use an explicit Span Quantifier:

```python
expect(selection).to(
    occur_once(),
    have_attribute("payment.currency", "GBP"),
)

expect(selection).to(
    any_span(have_attribute("payment.currency", "GBP")),
)

expect(selection).to(
    every_span(succeed()),
)
```

Applying a per-Span condition without singular cardinality or a quantifier is an ambiguous Expectation and raises a clear usage error.

### Observation correlation

A Span belongs to an Observation when it starts with that Observation's internal correlation context active. Correlation metadata is library-owned and must not be added to exported Span attributes or become part of Domain Telemetry.

The Python capture adapter can implement this with a `ContextVar` recorded synchronously when each Span starts, retaining an internal Span-to-Observation association until the Span ends. The semantic rule—not `ContextVar` itself—belongs to the core design.

If copied correlation context starts a Span after the Observation closes, the router records a Late Span violation and quarantines it. Span-processor callbacks must not raise into application code.

### Multiple Traces

A Span Selection searches the whole Observation by default, even when it contains several OpenTelemetry Traces. Relationship Expectations such as parent, child, ancestor, descendant, and ordered flow must be satisfied within a single Trace. Following OpenTelemetry Span Links across Traces requires an explicit future API and is outside V1.

### Python integration

The Testing Library is responsible for:

- connecting its capture adapter to an explicitly supplied OpenTelemetry SDK provider;
- defining Observation boundaries and lifecycle;
- correlating completed Spans to the correct Observation;
- normalising SDK data into the core model;
- selecting Spans and evaluating Expectations;
- rendering failure diagnostics as ordinary Python exceptions.

The caller is responsible for invoking this API from its chosen Host Test Framework or harness. The library does not expose framework fixtures, hooks, discovery, or runner behaviour.

## Hard design problems

### Tracer provider ownership

OpenTelemetry applications may already configure a global tracer provider. The caller must explicitly provide the SDK provider used by the application when creating a Telemetry Session. The library attaches its capture adapter to that provider but never replaces or shuts it down ([ADR 0003](docs/adr/0003-require-an-explicit-opentelemetry-provider.md)).

The Python SDK exposes processor addition but no public processor removal operation. The library therefore installs at most one reusable Observation Router per provider. Sessions register their active Observations with that router and unregister on close; the router remains attached but inert when unused ([ADR 0004](docs/adr/0004-reuse-one-observation-router-per-provider.md)).

### Test isolation

Spans may outlive the operation that started them, especially with background work. Clearing an exporter immediately before a call does not prove that all later spans belong to that call. Potential mechanisms include:

- capture timestamps;
- trace IDs returned or propagated by the driving call;
- a test-specific baggage value or attribute;
- explicit start/stop capture scopes;
- waiting until matching traces are quiescent.

V1 must document the concurrency guarantees it can actually provide.

At the Completion Point, any correlated Span that has started but not ended makes the Observation incomplete and causes an immediate `ObservationIncompleteError`. The library does not sleep in the hope that hidden work will finish.

A Span starting later from copied correlation context cannot be added to the immutable Observation. It becomes a quarantined Late Span, emits `LateSpanWarning`, marks the Observation invalid for subsequent access, and is checked again at Telemetry Session close.

### Diagnostics without framework integration

The library uses ordinary Python diagnostics:

- synchronous API contract failures raise typed exceptions;
- a Late Span detected inside a processor callback emits `LateSpanWarning` through Python's `warnings` system;
- closing a Telemetry Session raises an aggregate error for unresolved Lifecycle Violations;
- Expectation failures raise `TraceAssertionError` with rendered evidence.

There is no mandatory logging dependency or Host Test Framework hook. Callers can use standard Python warning filters to promote warnings to errors.

### Asynchronous completion

Export occurs when spans end, so assertions can race with asynchronous operations. V1 requires the driven operation and all relevant explicitly awaited work to complete before leaving the Observation scope. It will not infer completion of detached work. A later version may support bounded polling:

```python
expect(observation.spans.named("email.send")).eventually(timeout="2s").to(
    occur_once(),
)
```

This should be deliberate rather than an implicit sleep on every assertion.

### Semantic stability

Span names and attributes become part of a behavioural contract when tests assert them. The project should encourage stable, domain-oriented telemetry and OpenTelemetry semantic conventions where applicable, without pretending every library-generated span is a public contract.

### Sampling and limits

Tests require complete evidence. Test configuration should normally use always-on sampling and span limits large enough for asserted attributes and events. The library should detect common configuration mistakes and report them clearly where possible.

### Other telemetry signals

V1 supports OpenTelemetry Traces and Spans only. Logs and metrics are deferred without a compatibility promise. The Observation vocabulary leaves conceptual room for other evidence, but the implementation should not introduce generic multi-signal abstractions until a concrete use case requires them.

## Runtime compatibility

V1 requires Python 3.11 or newer and is tested on Python 3.11 through 3.14.

The initial dependency policy is:

```toml
requires-python = ">=3.11"
dependencies = ["opentelemetry-sdk>=1.44,<2"]
```

Declare `opentelemetry-api>=1.44,<2` separately only if the library imports it directly. Do not directly constrain `opentelemetry-semantic-conventions`; it remains an SDK implementation dependency.

## Competitive position

The idea belongs to an established category, but the proposed product shape is narrower.

- [Tracetest](https://tracetest.io/) is the primary product reference: a broader trace-based integration and end-to-end testing platform with triggers, infrastructure, a CLI/UI, and declarative test definitions.
- [Malabi](https://github.com/aspecto-io/malabi) is the closest developer-experience precedent: code-first trace assertions for JavaScript tests.
- OpenTelemetry Python's [`InMemorySpanExporter`](https://github.com/open-telemetry/opentelemetry-python/blob/main/opentelemetry-sdk/src/opentelemetry/sdk/trace/export/in_memory_span_exporter.py) provides the basic capture mechanism but not a behavioural assertion language.
- [`pytest-otel`](https://pypi.org/project/pytest-otel/) emits telemetry about pytest execution; it does not provide the proposed application-Trace assertion API.

The intended position is:

> A tiny, code-first, framework-agnostic Python behavioural testing library built on OpenTelemetry Traces.

The differentiation is primarily ergonomics, scope, and testing philosophy—not the invention of trace-based testing itself.

## V1 feature slice

A useful first release can be deliberately small:

1. Normalise completed `ReadableSpan` objects into an internal immutable model.
2. Group spans by trace ID and construct parent-child indexes.
3. Select Spans by exact name and attributes, with pattern matching deferred to explicit future syntax.
4. Assert `exists`, `did_not_occur`, `occurred_once`, and `count`.
5. Assert attributes, status, events, and duration.
6. Assert parent/child and ancestor/descendant relationships.
7. Provide explicit capture/reset semantics.
8. Render actionable failures with a relevant trace tree.
9. Expose capture and assertion APIs as ordinary Python context managers, objects, functions, and exceptions.

Anything beyond this slice should be justified by a concrete test that cannot be expressed cleanly.

## Possible package structure

```text
src/<package_name>/
    __init__.py
    model.py
    normalise.py
    query.py
    assertions.py
    diagnostics.py
    sources/
        base.py
        in_memory.py
    capture.py

tests/
    unit/
        test_normalise.py
        test_query.py
        test_assertions.py
        test_diagnostics.py
    integration/
        test_capture.py
        test_instrumented_application.py
```

This is a sketch, not a commitment. Module boundaries should be chosen around stable responsibilities once the first walking-skeleton test exists.

## Delivery sequence

### Milestone 1: walking skeleton

- Instrument a tiny example application.
- Capture one completed Span through the ordinary Python API.
- Make `expect(observation.spans.named("operation")).to(exist())` pass and fail.
- Show a custom failure message.

### Milestone 2: useful span assertions

- Add selection by attributes.
- Add count, status, event, and duration assertions.
- Handle multiple candidates predictably.
- Add a compact candidate-difference report.

### Milestone 3: trace structure

- Build trace trees from identifiers.
- Add relationship matchers.
- Render relevant trace subtrees on failure.

### Milestone 4: production-quality Python integration

- Finalise Observation and capture semantics.
- Support existing tracer-provider configuration.
- Define asynchronous waiting behaviour.
- Document isolation and concurrency limits.

### Milestone 5: validation

- Exercise the library against a realistic ports-and-adapters example.
- Compare representative tests with mocks, raw `ReadableSpan` inspection, Malabi, and Tracetest.
- Decide whether the API is sufficiently valuable and distinct to publish.

## Success criteria

The concept is validated if:

- a developer can add it to an instrumented Python application with little configuration;
- common behavioural assertions are materially clearer than manual span inspection;
- tests survive internal refactoring when semantic telemetry remains stable;
- a failed test makes the trace useful for diagnosis rather than adding noise;
- the library does not require teams to adopt a separate testing platform;
- the core model can plausibly accept OTLP data later without an API rewrite.

## Risks

- Tests may simply move coupling from implementation APIs to unstable span names and attributes.
- Global OpenTelemetry configuration can make automatic setup unreliable or intrusive.
- Async and concurrent applications may make span ownership and completion nondeterministic.
- Negative assertions are only trustworthy when capture completeness is guaranteed.
- Timing assertions can be flaky in CI.
- Existing tools may cover enough of the need that a separate library has limited adoption.
- A fluent API can become complicated faster than the underlying problem warrants.

The design should answer these risks through constrained semantics and good diagnostics rather than hiding them behind convenience.

## Open decisions

1. What behaviour-led package name is both accurate and available?
2. Which concrete conditions and selectors belong in the minimum viable assertion API?
3. What equality and type-coercion rules apply to OpenTelemetry attribute values?
4. How should failure diagnostics rank the closest candidate Span?

## Questions to validate with a prototype

- Does a fluent chain feel natural once multiple spans share a name?
- Can failure output identify a useful "closest" candidate without becoming noisy?
- Can capture scopes isolate Spans reliably under concurrent Host Test Framework execution?
- How much OpenTelemetry setup can be automatic without mutating global state unexpectedly?
- Is tree matching necessary for V1, or do relationship predicates cover the first real use cases?
- Do users want to assert a sequence, a parent-child graph, or both?

## Current decisions

These are working decisions for the first prototype, not permanent commitments:

- Python is the first frontend.
- The product is a framework-agnostic Testing Library, not a plugin or test runner ([ADR 0002](docs/adr/0002-build-a-framework-agnostic-testing-library.md)).
- A Telemetry Session requires an explicitly supplied OpenTelemetry SDK provider and never implicitly replaces or shuts down a global provider ([ADR 0003](docs/adr/0003-require-an-explicit-opentelemetry-provider.md)).
- Each provider receives at most one reusable Observation Router; Telemetry Sessions register with and reuse it ([ADR 0004](docs/adr/0004-reuse-one-observation-router-per-provider.md)).
- Telemetry Sessions are context managers with an equivalent explicit `close()`; closure rejects active Observations, checks violations, unregisters the Session, and never shuts down the provider.
- The primary V1 job is replacing mock-coupled behavioural assertions in in-process component tests.
- Applications must already emit meaningful Domain Telemetry; instrumentation helpers are outside V1.
- Asserted Domain Telemetry forms an application Behavioural Contract.
- An Observation is the bounded evidence for a test action and may contain multiple OpenTelemetry Traces.
- An Observation becomes immutable at an explicit Completion Point after synchronous and explicitly awaited work finishes.
- V1 does not claim to detect completion of detached background work.
- Closing an Observation with correlated open Spans raises `ObservationIncompleteError`; there is no implicit grace period.
- Expectations can only be evaluated against a closed Observation.
- Late Spans are quarantined, warn, and invalidate subsequent access to their Observation.
- Same-context nested Observations raise `NestedObservationError`; separate task or thread contexts may overlap.
- The capture, selection, Expectation, and diagnostic APIs are ordinary Python and give no Host Test Framework privileged status.
- A Span belongs to an Observation when it starts with that Observation's internal correlation context active.
- Internal correlation metadata never becomes part of exported telemetry or the Behavioural Contract.
- Span names are matched exactly by default; future pattern selection must use distinct syntax.
- A Span Selection and its Expectation are separate; all conditions in an Expectation are evaluated together.
- Per-Span conditions require singular cardinality or an explicit `any_span`/`every_span` quantifier.
- Basic Span Selections search the whole Observation; relationship Expectations must resolve within one Trace.
- `have_children` is an unordered subset relationship; exact children and temporal order use distinct Expectations.
- OpenTelemetry traces are the only signal in V1.
- OpenTelemetry logs and metrics are deferred without a compatibility promise.
- V1 requires Python 3.11+, tests through Python 3.14, and depends on `opentelemetry-sdk>=1.44,<2`.
- Lifecycle problems use typed exceptions and Python warnings; there is no required logging or Host Test Framework integration.
- `InMemorySpanExporter` is the first span source.
- The core operates on a normalised internal trace model.
- Matching is non-strict by default.
- Mock vocabulary is excluded from the public API: Spans `occur` rather than being `called`.
- Exact span-name matching is the default.
- Extra Spans, attributes, and events are accepted unless exact matching is explicitly requested.
- Negative assertions and diagnostic output are first-class features.
- OTLP ingestion is deferred until the in-process developer experience proves useful.

## Next step

Build a walking skeleton around one representative test:

```python
def test_declined_payment_is_not_persisted(client, telemetry):
    with telemetry.observe() as observation:
        response = client.post(
            "/payments",
            json={"amount": 1000, "currency": "GBP"},
        )

    assert response.status_code == 402
    expect(observation.spans.named("payment.authorise")).to(occur_once())
    expect(observation.spans.named("payment.gateway.authorise")).to(fail())
    expect(observation.spans.named("payment.persist")).to(not_occur())
```

That slice will force decisions about setup, capture boundaries, normalisation, selection, terminal assertions, negative evidence, and failure messages without requiring the full DSL up front.
