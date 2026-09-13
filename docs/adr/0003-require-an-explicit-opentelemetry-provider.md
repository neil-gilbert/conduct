---
status: accepted
---

# Require an explicit OpenTelemetry provider

A Telemetry Session will be created with an explicitly supplied OpenTelemetry SDK provider. The Testing Library will connect its capture adapter to that provider but will never discover, replace, or shut down a global provider implicitly. This makes ownership and side effects visible, avoids relying on Python's one-shot global provider configuration, and allows applications to retain control of their existing instrumentation at the cost of one explicit setup step.
