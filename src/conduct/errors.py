"""Ordinary Python errors; no host test framework integration is required."""

from dataclasses import dataclass


class TraceAssertionError(AssertionError):
    """The observation did not satisfy a behavioural expectation."""


class ExpectationUsageError(ValueError):
    """An expectation is ambiguous or malformed."""


class TelemetryConfigurationError(ValueError):
    """The supplied provider cannot guarantee complete evidence."""


class ObservationStateError(RuntimeError):
    """The operation is not valid at this point in the observation lifecycle."""


class NestedObservationError(ObservationStateError):
    """Observations cannot nest on the same execution context."""


class TelemetryClosedError(ObservationStateError):
    """The telemetry session has already closed."""


class ActiveObservationError(ObservationStateError):
    """A session cannot close until its active observations close."""


@dataclass(frozen=True)
class LifecycleViolation:
    observation_id: str
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"Observation {self.observation_id}: {self.kind}: {self.detail}"


class ObservationInvalidError(ObservationStateError):
    def __init__(self, violations: tuple[LifecycleViolation, ...]):
        self.violations = violations
        super().__init__("Observation evidence is invalid:\n" + "\n".join(map(str, violations)))


class ObservationIncompleteError(ObservationInvalidError):
    """Correlated spans were still open at the completion point."""


class TelemetryLifecycleError(ObservationStateError):
    def __init__(self, violations: tuple[LifecycleViolation, ...]):
        self.violations = violations
        super().__init__("Unresolved lifecycle violations:\n" + "\n".join(map(str, violations)))


class LateSpanWarning(RuntimeWarning):
    """Copied observation context started work after its completion point."""
