import asyncio
import contextvars
import gc
import os
import threading
import unittest
import warnings
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ConcurrentMultiSpanProcessor, SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind as OtelSpanKind

from conduct import (
    ActiveObservationError,
    LateSpanWarning,
    NestedObservationError,
    ObservationIncompleteError,
    ObservationInvalidError,
    ObservationStateError,
    SpanKind,
    SpanStatus,
    Telemetry,
    TelemetryClosedError,
    TelemetryConfigurationError,
    TelemetryLifecycleError,
    any_span,
    expect,
    have_attribute,
    not_occur,
    occur,
    occur_once,
)


class CountingProvider(TracerProvider):
    def __init__(self):
        super().__init__(sampler=ALWAYS_ON)
        self.additions = []

    def add_span_processor(self, processor):
        self.additions.append(processor)
        super().add_span_processor(processor)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.provider = CountingProvider()
        self.addCleanup(self.provider.shutdown)
        self.tracer = self.provider.get_tracer("tests")
        self.telemetry = Telemetry(provider=self.provider)
        self.addCleanup(self.telemetry.close)

    def emit(self, name="operation", **kwargs):
        with self.tracer.start_as_current_span(name, **kwargs):
            pass

    def test_walking_skeleton_and_unrelated_spans(self):
        self.emit("outside")
        with self.telemetry.observe() as observation:
            self.emit()
        self.emit("outside")
        expect(observation.spans.named("operation")).to(occur_once())
        expect(observation.spans.named("outside")).to(not_occur())

    def test_normalises_identity_parent_events_status_and_time(self):
        with self.telemetry.observe() as observation:
            with self.tracer.start_as_current_span("root", start_time=10) as root:
                child = self.tracer.start_span("child", kind=OtelSpanKind.CLIENT, start_time=20)
                child.set_attribute("tags", ["a", "b"])
                child.add_event("event", {"reason": "declined"}, timestamp=25)
                child.set_status(trace.Status(trace.StatusCode.ERROR, "declined"))
                child.end(end_time=30)
            root_context = root.get_span_context()
        captured = list(observation.spans.named("child"))[0]
        self.assertEqual(captured.trace_id, f"{root_context.trace_id:032x}")
        self.assertEqual(captured.parent_span_id, f"{root_context.span_id:016x}")
        self.assertEqual(captured.kind, SpanKind.CLIENT)
        self.assertEqual(captured.status, SpanStatus.ERROR)
        self.assertEqual(captured.status_description, "declined")
        self.assertEqual(captured.duration_ns, 10)
        self.assertEqual(captured.events[0].timestamp_ns, 25)
        self.assertEqual(captured.attributes["tags"], ("a", "b"))

    def test_all_evidence_reads_require_completion(self):
        with self.telemetry.observe() as observation:
            selection = observation.spans.named("operation")
            self.emit()
            for read in (
                lambda: expect(selection).to(occur_once()),
                lambda: expect(selection.named("absent")).to(not_occur()),
                lambda: len(selection),
                lambda: list(selection),
                lambda: observation.traces,
            ):
                with self.subTest(read=read), self.assertRaises(ObservationStateError):
                    read()
        expect(selection).to(occur_once())

    def test_multiple_traces_and_fresh_observations(self):
        with self.telemetry.observe() as first:
            self.emit("first", context=Context())
            self.emit("second", context=Context())
        self.assertEqual(len(first.traces), 2)
        expect(first.spans).to(occur(2))
        with self.telemetry.observe() as second:
            self.emit("third")
        expect(second.spans.named("first")).to(not_occur())
        expect(first.spans.named("third")).to(not_occur())

    def test_span_started_before_boundary_is_excluded_even_if_it_ends_inside(self):
        outside = self.tracer.start_span("outside")
        with self.telemetry.observe() as observation:
            outside.end()
            self.emit()
        expect(observation.spans).to(occur_once())

    def test_open_spans_fail_immediately_and_never_mutate_evidence_on_later_end(self):
        with self.assertRaises(ObservationIncompleteError) as raised:
            with self.telemetry.observe() as observation:
                open_span = self.tracer.start_span("unfinished")
                self.emit("finished")
        self.assertIn("unfinished", str(raised.exception))
        self.assertIn("trace=", str(raised.exception))
        open_span.end()
        with self.assertRaises(ObservationInvalidError):
            expect(observation.spans).to(occur_once())
        self.assertFalse(self.provider.additions[0].pending)

    def test_late_spans_warn_invalidate_cached_selections_and_are_quarantined(self):
        with self.telemetry.observe() as observation:
            copied = contextvars.copy_context()
            self.emit()
        selection = observation.spans
        expect(selection).to(occur_once())
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", LateSpanWarning)
            copied.run(self.emit, "late")
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, LateSpanWarning)
        for read in (lambda: len(selection), lambda: observation.traces):
            with self.assertRaises(ObservationInvalidError):
                read()
        self.assertEqual(len(observation._graph.spans), 1)
        self.assertFalse(self.provider.additions[0].pending)

    def test_warning_promoted_to_error_does_not_escape_sdk_callback(self):
        with self.telemetry.observe():
            copied = contextvars.copy_context()
        with warnings.catch_warnings():
            warnings.simplefilter("error", LateSpanWarning)
            copied.run(self.emit, "late")
        with self.assertRaisesRegex(TelemetryLifecycleError, "late"):
            self.telemetry.close()
        with self.assertRaises(TelemetryClosedError):
            self.telemetry.observe()

    def test_session_close_aggregates_unreported_violations_from_several_observations(self):
        contexts = []
        for _ in range(2):
            with self.telemetry.observe():
                contexts.append(contextvars.copy_context())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", LateSpanWarning)
            for copied in contexts:
                copied.run(self.emit)
        with self.assertRaises(TelemetryLifecycleError) as raised:
            self.telemetry.close()
        self.assertEqual(len(raised.exception.violations), 2)
        self.assertFalse(self.provider.additions[0].sessions)

    def test_late_context_still_invalidates_evidence_after_session_closes(self):
        with self.telemetry.observe() as observation:
            copied = contextvars.copy_context()
        self.telemetry.close()
        with self.assertWarns(LateSpanWarning):
            copied.run(self.emit)
        with self.assertRaises(ObservationInvalidError):
            len(observation.spans)

    def test_nested_observations_across_sessions_are_rejected_and_outer_survives(self):
        with Telemetry(provider=self.provider) as other, self.telemetry.observe() as observation:
            for session in (self.telemetry, other):
                with self.assertRaises(NestedObservationError), session.observe():
                    pass
            self.emit()
        expect(observation.spans).to(occur_once())

    def test_session_close_rejects_active_observations_without_closing(self):
        with self.telemetry.observe() as observation:
            with self.assertRaises(ActiveObservationError):
                self.telemetry.close()
            self.emit()
        self.telemetry.close()
        expect(observation.spans).to(occur_once())
        self.telemetry.close()  # Idempotent.

    def test_single_use_observations_and_scopes_created_before_session_close(self):
        scope = self.telemetry.observe()
        with self.assertRaises(ObservationStateError):
            scope.close()
        with scope:
            pass
        scope.close()
        with self.assertRaises(ObservationStateError), scope:
            pass
        unused = self.telemetry.observe()
        self.telemetry.close()
        with self.assertRaises(TelemetryClosedError), unused:
            pass

    def test_close_from_wrong_context_does_not_corrupt_scope(self):
        with self.telemetry.observe() as observation:
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(ObservationStateError):
                    pool.submit(observation.close).result()
            self.emit()
        expect(observation.spans).to(occur_once())

    def test_application_errors_propagate_and_context_is_restored(self):
        with self.assertRaisesRegex(ValueError, "application"):
            with self.telemetry.observe() as observation:
                self.emit()
                raise ValueError("application")
        expect(observation.spans).to(occur_once())
        with self.telemetry.observe():
            pass

    def test_application_and_incomplete_errors_are_both_preserved(self):
        with self.assertRaises(ExceptionGroup) as raised:
            with self.telemetry.observe():
                unfinished = self.tracer.start_span("unfinished")
                raise ValueError("application")
        unfinished.end()
        self.assertIsInstance(raised.exception.exceptions[0], ValueError)
        self.assertIsInstance(raised.exception.exceptions[1], ObservationIncompleteError)

    def test_one_router_is_reused_without_shutting_down_provider_or_polluting_attributes(self):
        exporter = InMemorySpanExporter()
        self.provider.add_span_processor(SimpleSpanProcessor(exporter))
        global_provider = trace.get_tracer_provider()
        for _ in range(20):
            with Telemetry(provider=self.provider) as session:
                with session.observe() as observation:
                    self.emit(attributes={"domain.key": "value"})
                expect(observation.spans).to(occur_once())
        self.assertEqual(len(self.provider.additions), 2)
        self.emit("still-running")
        self.assertEqual(len(exporter.get_finished_spans()), 21)
        self.assertEqual(dict(exporter.get_finished_spans()[0].attributes), {"domain.key": "value"})
        self.assertIs(trace.get_tracer_provider(), global_provider)
        self.assertTrue(self.provider.force_flush())

    def test_different_providers_do_not_capture_each_others_spans(self):
        provider = TracerProvider(sampler=ALWAYS_ON)
        self.addCleanup(provider.shutdown)
        other_tracer = provider.get_tracer("other")
        with Telemetry(provider=provider), self.telemetry.observe() as observation:
            with other_tracer.start_as_current_span("other"):
                self.emit("ours")
        expect(observation.spans).to(occur_once())
        expect(observation.spans.named("other")).to(not_occur())

    def test_known_incomplete_sampling_configurations_are_rejected(self):
        for sampler in (ALWAYS_OFF, ParentBased(ALWAYS_ON), TraceIdRatioBased(0.5)):
            provider = TracerProvider(sampler=sampler)
            self.addCleanup(provider.shutdown)
            with self.subTest(sampler=sampler), self.assertRaises(TelemetryConfigurationError):
                Telemetry(provider=provider)
        with self.assertRaises(TelemetryConfigurationError):
            Telemetry(provider=None)

    def test_disabled_sdk_is_rejected_instead_of_returning_empty_evidence(self):
        with patch.dict(os.environ, {"OTEL_SDK_DISABLED": "true"}):
            provider = TracerProvider(sampler=ALWAYS_ON)
        self.addCleanup(provider.shutdown)
        with self.assertRaisesRegex(TelemetryConfigurationError, "OTEL_SDK_DISABLED"):
            Telemetry(provider=provider)

    def test_concurrent_sdk_dispatch_is_rejected_before_losing_correlation(self):
        provider = TracerProvider(
            sampler=ALWAYS_ON, active_span_processor=ConcurrentMultiSpanProcessor()
        )
        self.addCleanup(provider.shutdown)
        with self.assertRaisesRegex(TelemetryConfigurationError, "synchronous processor pipeline"):
            Telemetry(provider=provider)

    def test_dropped_attributes_events_and_event_attributes_invalidate_evidence(self):
        cases = [
            (SpanLimits(max_attributes=0), lambda s: s.set_attribute("key", "value")),
            (SpanLimits(max_events=0), lambda s: s.add_event("event")),
            (SpanLimits(max_event_attributes=0), lambda s: s.add_event("event", {"key": "value"})),
        ]
        for limits, emit in cases:
            provider = TracerProvider(sampler=ALWAYS_ON, span_limits=limits)
            self.addCleanup(provider.shutdown)
            with self.subTest(limits=limits), Telemetry(provider=provider) as session:
                with self.assertRaisesRegex(ObservationInvalidError, "dropped telemetry"):
                    with session.observe():
                        with provider.get_tracer("limited").start_as_current_span(
                            "limited"
                        ) as item:
                            emit(item)

    def test_provider_shutdown_during_observation_invalidates_even_empty_evidence(self):
        with self.assertRaisesRegex(ObservationInvalidError, "provider shutdown"):
            with self.telemetry.observe():
                self.provider.shutdown()
        with self.assertRaises(TelemetryConfigurationError):
            self.telemetry.observe()

    def test_provider_registry_does_not_keep_a_closed_session_or_provider_alive(self):
        provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
        with Telemetry(provider=provider) as session:
            pass
        provider_ref, session_ref = weakref.ref(provider), weakref.ref(session)
        del provider, session
        gc.collect()
        self.assertIsNone(provider_ref())
        self.assertIsNone(session_ref())

    def test_concurrent_threads_isolate_observations_and_share_one_router(self):
        barrier = threading.Barrier(6)

        def run(index):
            with Telemetry(provider=self.provider) as session, session.observe() as observation:
                barrier.wait(timeout=5)
                for _ in range(5):
                    self.emit("work", attributes={"owner": index})
            expect(observation.spans).to(occur(5), any_span(have_attribute("owner", index)))
            self.assertEqual({s.attributes["owner"] for s in observation.spans}, {index})

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(run, range(6)))
        self.assertEqual(len(self.provider.additions), 1)

    def test_span_can_end_in_another_thread_without_correlation_context(self):
        with self.telemetry.observe() as observation:
            item = self.tracer.start_span("work")
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(item.end).result()
        expect(observation.spans).to(occur_once())

    def test_async_tasks_isolate_observations_and_awaited_work_inherits_context(self):
        async def run():
            async def worker(index):
                with self.telemetry.observe() as observation:
                    with self.tracer.start_as_current_span(f"root-{index}"):
                        await asyncio.sleep(0)
                        await asyncio.to_thread(self.emit, "thread", attributes={"owner": index})

                        async def child():
                            await asyncio.sleep(0)
                            self.emit("child", attributes={"owner": index})

                        await asyncio.create_task(child())
                expect(observation.spans).to(occur(3))
                expect(observation.spans.named("child")).to(
                    occur_once(), have_attribute("owner", index)
                )
                self.assertEqual(len(observation.traces), 1)

            await asyncio.gather(*(worker(index) for index in range(8)))

        asyncio.run(run())

    def test_async_detached_late_work_is_detected(self):
        async def run():
            release = asyncio.Event()

            async def detached():
                await release.wait()
                self.emit("late")

            with self.telemetry.observe() as observation:
                task = asyncio.create_task(detached())
            release.set()
            with self.assertWarns(LateSpanWarning):
                await task
            with self.assertRaises(ObservationInvalidError):
                expect(observation.spans).to(not_occur())

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
