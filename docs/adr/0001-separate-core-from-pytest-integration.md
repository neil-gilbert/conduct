---
status: superseded by ADR-0002
---

# Separate the assertion core from pytest integration

The first product will target in-process Python component tests, but the trace model and assertion engine will be a standalone core over supplied telemetry data. pytest will remain a thin adapter responsible for test lifecycle and OpenTelemetry integration. This costs a small amount of boundary design now, but prevents pytest and SDK configuration details from becoming inseparable from the assertion semantics, preserving a credible path to OTLP sources and other test frontends later.
