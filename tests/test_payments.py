import unittest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from conduct import (
    Telemetry,
    TraceAssertionError,
    any_span,
    be_descendant_of,
    every_span,
    expect,
    fail,
    have_attribute,
    have_children,
    have_event,
    not_occur,
    occur,
    occur_in_order,
    occur_once,
    succeed,
)
from examples.payments import MemoryStore, Payments, ProviderUnavailable, SandboxGateway


class PaymentTests(unittest.TestCase):
    def setUp(self):
        self.provider = TracerProvider(sampler=ALWAYS_ON)
        self.addCleanup(self.provider.shutdown)
        self.telemetry = Telemetry(provider=self.provider)
        self.addCleanup(self.telemetry.close)

    def application(self, outcomes):
        return Payments(
            self.provider.get_tracer("payments"), SandboxGateway(outcomes), MemoryStore()
        )

    def test_declined_payment_is_not_persisted(self):
        app = self.application([False])
        with self.telemetry.observe() as observation:
            response = app.submit(1000, "GBP")
        self.assertEqual(response.status_code, 402)
        expect(observation.spans.named("payment.authorise")).to(
            occur_once(),
            have_attribute("payment.amount", 1000),
            have_event("payment.declined", reason="insufficient_funds"),
        )
        expect(observation.spans.named("payment.gateway.authorise")).to(occur_once(), fail())
        expect(observation.spans.named("payment.persist")).to(not_occur())

    def test_suspended_account_never_contacts_provider(self):
        app = self.application([])
        with self.telemetry.observe() as observation:
            response = app.submit(1000, "GBP", suspended=True)
        self.assertEqual(response.status_code, 403)
        expect(observation.spans.named("payment.gateway.authorise")).to(not_occur())
        expect(observation.spans.named("payment.persist")).to(not_occur())

    def test_transient_failure_retries_once_then_persists(self):
        app = self.application([ProviderUnavailable("try again"), True])
        with self.telemetry.observe() as observation:
            response = app.submit(1000, "GBP")
        self.assertEqual(response.status_code, 201)
        expect(observation.spans.named("payment.gateway.authorise")).to(
            occur(2),
            any_span(fail()),
            any_span(succeed()),
            every_span(
                be_descendant_of("payment.authorise"), have_attribute("payment.currency", "GBP")
            ),
        )
        expect(observation.spans.named("payment.authorise")).to(
            occur_once(),
            have_children("customer.lookup", "payment.gateway.authorise", "payment.persist"),
        )
        expect(observation.spans).to(
            occur_in_order(
                "customer.lookup",
                "payment.gateway.authorise",
                "payment.gateway.authorise",
                "payment.persist",
            )
        )

    def test_persistent_provider_failure_stops_after_one_retry(self):
        app = self.application([ProviderUnavailable("down"), ProviderUnavailable("still down")])
        with self.telemetry.observe() as observation:
            response = app.submit(1000, "GBP")
        self.assertEqual(response.status_code, 503)
        expect(observation.spans.named("payment.gateway.authorise")).to(
            occur(2), every_span(fail())
        )
        expect(observation.spans.named("payment.persist")).to(not_occur())

    def test_failure_renders_the_domain_workflow_with_attribute_differences(self):
        with self.telemetry.observe() as observation:
            self.application([True]).submit(1000, "EUR")
        with self.assertRaises(TraceAssertionError) as raised:
            expect(observation.spans.named("payment.gateway.authorise")).to(
                occur_once(), have_attribute("payment.currency", "GBP")
            )
        for evidence in (
            "GBP",
            "EUR",
            "payment.authorise",
            "customer.lookup",
            "payment.persist",
            "<-- candidate",
            "Observed trace",
            "span=",
        ):
            self.assertIn(evidence, str(raised.exception))
