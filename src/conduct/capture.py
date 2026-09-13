"""Explicit observation boundaries and one reusable router per SDK provider."""

from __future__ import annotations

import warnings
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from types import TracebackType
from uuid import uuid4
from weakref import WeakKeyDictionary, WeakSet

from opentelemetry.context import Context
from opentelemetry.sdk.trace import (
    ReadableSpan,
    Span,
    SpanProcessor,
    SynchronousMultiSpanProcessor,
    Tracer,
    TracerProvider,
)
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from .errors import (
    ActiveObservationError,
    LateSpanWarning,
    LifecycleViolation,
    NestedObservationError,
    ObservationIncompleteError,
    ObservationInvalidError,
    ObservationStateError,
    TelemetryClosedError,
    TelemetryConfigurationError,
    TelemetryLifecycleError,
)
from .model import Trace, TraceGraph
from .query import SpanSelection
from .sources.in_memory import InMemorySource

_current: ContextVar[Observation | None] = ContextVar("conduct_observation", default=None)
_registry: WeakKeyDictionary[TracerProvider, _ObservationRouter] = WeakKeyDictionary()
_registry_lock = RLock()
SpanKey = tuple[int, int]


@dataclass
class _Violation:
    value: LifecycleViolation
    delivered: bool = False


def _key(span: Span | ReadableSpan) -> SpanKey:
    context = span.get_span_context()
    return context.trace_id, context.span_id


def _describe(span: Span | ReadableSpan) -> str:
    trace_id, span_id = _key(span)
    return f"{span.name!r} (trace={trace_id:032x}, span={span_id:016x})"


def _exit_with_close(close, body_error: BaseException | None) -> None:
    try:
        close()
    except Exception as lifecycle_error:
        if body_error is not None:
            raise BaseExceptionGroup(
                "Application error and telemetry lifecycle error", [body_error, lifecycle_error]
            ) from None
        raise


class Observation:
    """A single-use capture scope. Read evidence only after its completion point."""

    def __init__(self, session: Telemetry) -> None:
        self.id = uuid4().hex
        self._session = session
        self._router = session._router
        self._state = "new"
        self._token: Token | None = None
        self._open: dict[SpanKey, str] = {}
        self._source: InMemorySource | None = None
        self._graph = TraceGraph(())
        self._violations: list[_Violation] = []

    def __enter__(self) -> Observation:
        with self._router.lock:
            self._session._check_open()
            if self._state != "new":
                raise ObservationStateError("An observation can only be entered once")
            if _current.get() is not None:
                raise NestedObservationError(
                    "An observation correlation context is already active; "
                    "use an independent task or thread context for another observation"
                )
            self._source = InMemorySource()
            self._state = "active"
            self._session._active.add(self)
            self._token = _current.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _exit_with_close(self.close, exc)

    def _invalidate(self, kind: str, detail: str) -> None:
        violation = _Violation(LifecycleViolation(self.id, kind, detail))
        self._violations.append(violation)
        if not self._session._closed:
            self._session._unresolved.append(violation)

    def _invalid_error(self, error_type=ObservationInvalidError) -> ObservationInvalidError:
        for violation in self._violations:
            violation.delivered = True
        return error_type(tuple(violation.value for violation in self._violations))

    def close(self) -> None:
        with self._router.lock:
            if self._state == "closed":
                return
            if self._state != "active" or self._token is None:
                raise ObservationStateError("The observation has not been entered")
            try:
                _current.reset(self._token)
            except ValueError as error:
                raise ObservationStateError(
                    "An observation must close in the execution context that entered it"
                ) from error
            self._token = None
            self._state = "closed"
            self._session._active.remove(self)
            incomplete = bool(self._open)
            if incomplete:
                self._invalidate("open spans", "; ".join(self._open.values()))
            # Ending an incomplete span later must not retain or modify its observation.
            for key in self._open:
                self._router.pending.pop(key, None)
            self._open.clear()
            assert self._source is not None
            try:
                self._graph = self._source.snapshot()
            except Exception as error:
                self._invalidate("capture failed", str(error))
            finally:
                self._source.close()
                self._source = None
            if self._violations:
                error_type = ObservationIncompleteError if incomplete else ObservationInvalidError
                raise self._invalid_error(error_type)

    def _snapshot(self) -> TraceGraph:
        with self._router.lock:
            if self._state != "closed":
                raise ObservationStateError("Expectations require a closed observation")
            if self._violations:
                raise self._invalid_error()
            return self._graph

    @property
    def spans(self) -> SpanSelection:
        # Selections can be prepared in the scope, but resolving them always rechecks validity.
        return SpanSelection(self._snapshot)

    @property
    def traces(self) -> tuple[Trace, ...]:
        return tuple(self._snapshot().traces.values())


class _ObservationRouter(SpanProcessor):
    def __init__(self) -> None:
        self.lock = RLock()
        self.sessions: WeakSet[Telemetry] = WeakSet()
        self.pending: dict[SpanKey, Observation] = {}
        self.stopped = False

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        observation = _current.get()
        if observation is None or observation._router is not self:
            return
        late_message = None
        try:
            with self.lock:
                if observation._state == "closed":
                    late_message = f"Late span {_describe(span)} after observation {observation.id}"
                    observation._invalidate("late span", late_message)
                elif observation._state == "active" and not self.stopped:
                    key = _key(span)
                    self.pending[key] = observation
                    observation._open[key] = _describe(span)
            if late_message:
                # Even warnings promoted to errors must never escape a processor callback.
                warnings.warn(late_message, LateSpanWarning, stacklevel=2)
        except Exception as error:
            if late_message is None:
                with self.lock:
                    observation._invalidate("capture failed", str(error))

    def on_end(self, span: ReadableSpan) -> None:
        observation = None
        try:
            with self.lock:
                observation = self.pending.pop(_key(span), None)
                if observation is None:
                    return
                observation._open.pop(_key(span), None)
                if (
                    span.dropped_attributes
                    or span.dropped_events
                    or any(event.dropped_attributes for event in span.events)
                ):
                    observation._invalidate(
                        "dropped telemetry", f"{_describe(span)} exceeded attribute or event limits"
                    )
                assert observation._source is not None
                observation._source.append(span)
        except Exception as error:
            if observation is not None:
                with self.lock:
                    observation._invalidate("capture failed", str(error))

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True  # Export is synchronous at span end.

    def shutdown(self) -> None:
        with self.lock:
            self.stopped = True
            for session in self.sessions:
                for observation in session._active:
                    observation._invalidate(
                        "provider shutdown", "Provider stopped during observation"
                    )


class Telemetry:
    """Capture from an explicit, caller-owned, always-on SDK tracer provider."""

    def __init__(self, *, provider: TracerProvider) -> None:
        if not isinstance(provider, TracerProvider):
            raise TelemetryConfigurationError(
                "Supply the application's SDK TracerProvider explicitly"
            )
        if provider.sampler != ALWAYS_ON:
            raise TelemetryConfigurationError(
                "Complete observations require TracerProvider(sampler=ALWAYS_ON). "
                "Parent-based and custom samplers can hide behaviour from negative expectations."
            )
        tracer = provider.get_tracer("conduct.capture")
        if not isinstance(tracer, Tracer):
            raise TelemetryConfigurationError(
                "The provider returned a non-recording tracer; check OTEL_SDK_DISABLED"
            )
        if not isinstance(tracer.span_processor, SynchronousMultiSpanProcessor):
            raise TelemetryConfigurationError(
                "Observation correlation requires the SDK's synchronous processor pipeline; "
                "concurrent processor dispatch does not preserve the starting execution context"
            )
        self.provider = provider
        self._closed = False
        self._active: set[Observation] = set()
        self._unresolved: list[_Violation] = []
        with _registry_lock:
            router = _registry.get(provider)
            if router is None:
                router = _ObservationRouter()
                provider.add_span_processor(router)
                _registry[provider] = router
            self._router = router
        with router.lock:
            self._check_open()
            router.sessions.add(self)

    def _check_open(self) -> None:
        if self._closed:
            raise TelemetryClosedError("This telemetry session is closed")
        if self._router.stopped:
            raise TelemetryConfigurationError("The provider has been shut down")

    def __enter__(self) -> Telemetry:
        with self._router.lock:
            self._check_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _exit_with_close(self.close, exc)

    def observe(self) -> Observation:
        with self._router.lock:
            self._check_open()
            return Observation(self)

    def close(self) -> None:
        with self._router.lock:
            if self._closed:
                return
            if self._active:
                raise ActiveObservationError(
                    f"Cannot close session with {len(self._active)} active observation(s)"
                )
            unresolved = tuple(v.value for v in self._unresolved if not v.delivered)
            self._unresolved.clear()
            self._closed = True
            self._router.sessions.discard(self)
            if unresolved:
                raise TelemetryLifecycleError(unresolved)
