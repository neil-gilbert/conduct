---
status: accepted
---

# Reuse one Observation Router per provider

The Testing Library will attach at most one reusable Observation Router to each OpenTelemetry provider. Telemetry Sessions register and unregister their active Observations with that router; the router remains attached but inert when unused. This avoids accumulating processors across repeated Sessions despite the Python SDK exposing processor addition but no public removal operation, at the cost of maintaining a small provider-to-router registry and a long-lived inert processor.
