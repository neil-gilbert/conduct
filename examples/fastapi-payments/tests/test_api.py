"""Drive HTTP endpoints; make behavioural assertions with the installed Conduct package."""

import asyncio
import unittest

from conduct_payments.main import create_app
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from conduct import (
    Telemetry,
    TraceAssertionError,
    be_descendant_of,
    every_span,
    expect,
    fail,
    have_attribute,
    have_children,
    have_event,
    not_occur,
    occur_in_order,
    occur_once,
    succeed,
)


class PaymentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        provider = TracerProvider(sampler=ALWAYS_ON)
        self.addCleanup(provider.shutdown)
        app = create_app(provider=provider)
        # ASGITransport does not run lifespan automatically. Start the app explicitly.
        await self.enterAsyncContext(app.router.lifespan_context(app))
        self.telemetry = self.enterContext(Telemetry(provider=provider))
        self.client = await self.enterAsyncContext(
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        )

    async def test_accepted_payment_is_persisted_and_can_be_read_over_http(self):
        with self.telemetry.observe() as observation:
            response = await self.client.post(
                "/payments",
                json={"account_id": "active-account", "amount": 1000, "currency": "GBP"},
            )

        self.assertEqual(response.status_code, 201)
        payment = response.json()
        expect(observation.spans.named("payment.authorise")).to(
            occur_once(),
            succeed(),
            have_attribute("payment.amount", 1000),
            have_children("customer.lookup", "payment.gateway.authorise", "payment.persist"),
        )
        expect(observation.spans.named("payment.persist")).to(
            occur_once(),
            have_attribute("payment.id", payment["id"]),
            be_descendant_of("payment.authorise"),
        )
        expect(observation.spans).to(
            occur_in_order("customer.lookup", "payment.gateway.authorise", "payment.persist")
        )
        fetched = await self.client.get(f"/payments/{payment['id']}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json(), payment)

    async def test_declined_payment_is_never_persisted(self):
        with self.telemetry.observe() as observation:
            response = await self.client.post(
                "/payments",
                json={"account_id": "active-account", "amount": 20_000, "currency": "GBP"},
            )

        self.assertEqual(response.status_code, 402)
        expect(observation.spans.named("payment.gateway.authorise")).to(
            occur_once(), fail(), have_event("payment.declined", reason="insufficient_funds")
        )
        expect(observation.spans.named("payment.persist")).to(not_occur())

    async def test_suspended_account_never_contacts_the_payment_provider(self):
        with self.telemetry.observe() as observation:
            response = await self.client.post(
                "/payments", json={"account_id": "suspended-account", "amount": 1000}
            )

        self.assertEqual(response.status_code, 403)
        expect(observation.spans.named("payment.authorise")).to(
            occur_once(), have_event("account.suspended")
        )
        expect(observation.spans.named("payment.gateway.authorise")).to(not_occur())
        expect(observation.spans.named("payment.persist")).to(not_occur())

    async def test_unknown_account_is_rejected_before_authorisation(self):
        with self.telemetry.observe() as observation:
            response = await self.client.post(
                "/payments", json={"account_id": "unknown-account", "amount": 1000}
            )

        self.assertEqual(response.status_code, 404)
        expect(observation.spans.named("customer.lookup")).to(occur_once(), fail())
        expect(observation.spans.named("payment.gateway.authorise")).to(not_occur())
        expect(observation.spans.named("payment.persist")).to(not_occur())

    async def test_invalid_requests_do_not_enter_the_payment_workflow(self):
        for invalid in ({"amount": 0}, {"amount": True}, {"currency": "INVALID"}):
            with self.subTest(invalid=invalid):
                payload = {"account_id": "active-account", "amount": 1000, **invalid}
                with self.telemetry.observe() as observation:
                    response = await self.client.post("/payments", json=payload)
                self.assertEqual(response.status_code, 422)
                expect(observation.spans.named("payment.authorise")).to(not_occur())
                expect(observation.spans.named("payment.persist")).to(not_occur())

    async def test_unknown_payment_returns_404_with_lookup_evidence(self):
        with self.telemetry.observe() as observation:
            response = await self.client.get("/payments/missing")
        self.assertEqual(response.status_code, 404)
        expect(observation.spans.named("payment.lookup")).to(occur_once())

    async def test_concurrent_http_requests_keep_their_observations_separate(self):
        async def submit(currency):
            with self.telemetry.observe() as observation:
                response = await self.client.post(
                    "/payments",
                    json={"account_id": "active-account", "amount": 1000, "currency": currency},
                )
            self.assertEqual(response.status_code, 201)
            expect(observation.spans.named("payment.authorise")).to(
                occur_once(), have_attribute("payment.currency", currency)
            )
            expect(observation.spans.named("payment.gateway.authorise")).to(
                occur_once(), every_span(have_attribute("payment.currency", currency))
            )
            self.assertEqual(len(observation.traces), 1)
            return response.json()["id"]

        payment_ids = await asyncio.gather(submit("GBP"), submit("EUR"), submit("USD"))
        self.assertEqual(len(set(payment_ids)), 3)

    async def test_wrong_expectation_explains_the_observed_behaviour(self):
        with self.telemetry.observe() as observation:
            await self.client.post(
                "/payments",
                json={"account_id": "active-account", "amount": 1000, "currency": "EUR"},
            )

        with self.assertRaises(TraceAssertionError) as raised:
            expect(observation.spans.named("payment.gateway.authorise")).to(
                occur_once(), have_attribute("payment.currency", "GBP")
            )
        for evidence in ("GBP", "EUR", "payment.authorise", "payment.persist", "<-- candidate"):
            self.assertIn(evidence, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
