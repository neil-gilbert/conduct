# Telemetry-Native Behavioural Testing

This context defines the language used to describe behavioural evidence captured from an OpenTelemetry-instrumented system during a test.

## Language

**Observation**:
A bounded collection of telemetry evidence associated with one test action. An Observation may contain more than one OpenTelemetry Trace and becomes immutable at its Completion Point.
_Avoid_: Trace capture, captured trace, test trace

**Invalid Observation**:
A closed Observation whose completeness guarantee was violated, such as by a correlated Late Span. Expectations cannot be evaluated reliably against it.
_Avoid_: Failed observation, dirty observation

**Completion Point**:
The explicit moment after all synchronous and awaited work for an Observation has finished, after which its evidence is immutable and assertions may be evaluated. Detached background work is outside the V1 contract.
_Avoid_: Automatic quiescence, eventual completion

**Span Selection**:
A set of Spans from an Observation chosen by explicit criteria, such as an exact Span name or attribute value. A Span Selection contains no assertion by itself.
_Avoid_: Expected span, match

**Span Quantifier**:
An explicit rule stating whether an Expectation applies to exactly one, any, or every Span in a Span Selection. Per-Span conditions never imply a quantifier.
_Avoid_: Implicit match, automatic cardinality

**Expectation**:
One or more behavioural conditions evaluated together against a Span Selection, producing a single diagnostic result.
_Avoid_: Assertion chain, matcher chain

**Host Test Framework**:
Any external framework that discovers or executes tests using this library. The library does not require, extend, or provide special integration for a particular Host Test Framework.
_Avoid_: Calling pytest “the test framework” as though it were part of this product

**Testing Library**:
This framework-agnostic Python library for capturing Observations and evaluating Expectations. It does not discover tests, provide fixtures, or run test suites.
_Avoid_: pytest plugin, test runner, testing platform

**Telemetry Session**:
The Testing Library object connected to an explicitly supplied OpenTelemetry provider. It owns Observation creation and routes completed Spans without owning or replacing the provider.
_Avoid_: Global telemetry, test provider

**Lifecycle Violation**:
Evidence that a Telemetry Session or Observation did not honour its declared boundary, such as closing a Session with an active Observation or receiving a Late Span.
_Avoid_: Assertion failure, telemetry error

**Trace**:
An OpenTelemetry trace: a set of causally related Spans that share a trace identifier. A Trace is evidence within an Observation, not the Observation itself.
_Avoid_: Using “trace” for all telemetry captured by a test

**Span**:
A timed record of one operation within a Trace, including its identity, relationships, status, attributes, and events.

**Late Span**:
A Span correlated with an Observation but started after that Observation's Completion Point. A Late Span is quarantined and never added to the immutable Observation.
_Avoid_: Background span, delayed span

**Domain Telemetry**:
Deliberately designed, semantically meaningful telemetry that describes application behaviour and is suitable for assertions.
_Avoid_: Business telemetry, test telemetry

**Incidental Telemetry**:
Telemetry emitted by frameworks or implementation components that may aid diagnosis but is not part of the application’s Behavioural Contract.
_Avoid_: Noise

**Behavioural Contract**:
The stable set of Domain Telemetry names, attributes, events, and relationships that tests and other consumers may rely upon.
_Avoid_: Instrumentation contract, trace API
