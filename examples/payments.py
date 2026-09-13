"""A small instrumented application driven through its public payment port.

Run with ``python -m examples.payments`` after installing the project.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode, Tracer

from conduct import Telemetry, expect, fail, not_occur, occur_once


class ProviderUnavailable(Exception):
    pass


class Gateway(Protocol):
    def authorise(self, amount: int, currency: str) -> bool: ...


class Store(Protocol):
    def save(self, amount: int, currency: str) -> None: ...


class SandboxGateway:
    """A deterministic local payment-provider adapter for exercising the application."""

    def __init__(self, outcomes: Iterable[bool | Exception]) -> None:
        self._outcomes = iter(outcomes)

    def authorise(self, amount: int, currency: str) -> bool:
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class MemoryStore:
    def __init__(self) -> None:
        self.payments: list[tuple[int, str]] = []

    def save(self, amount: int, currency: str) -> None:
        self.payments.append((amount, currency))


@dataclass(frozen=True)
class PaymentResult:
    status_code: int


class Payments:
    def __init__(self, tracer: Tracer, gateway: Gateway, store: Store) -> None:
        self._tracer = tracer
        self._gateway = gateway
        self._store = store

    def submit(self, amount: int, currency: str, *, suspended: bool = False) -> PaymentResult:
        with self._tracer.start_as_current_span("payment.authorise") as operation:
            operation.set_attributes({"payment.amount": amount, "payment.currency": currency})
            with self._tracer.start_as_current_span("customer.lookup"):
                if suspended:
                    operation.add_event("account.suspended")
                    return PaymentResult(403)
            for attempt in range(2):
                try:
                    with self._tracer.start_as_current_span("payment.gateway.authorise") as request:
                        request.set_attributes(
                            {"payment.currency": currency, "retry.attempt": attempt}
                        )
                        accepted = self._gateway.authorise(amount, currency)
                        if not accepted:
                            request.set_status(Status(StatusCode.ERROR, "declined"))
                            operation.add_event(
                                "payment.declined", {"reason": "insufficient_funds"}
                            )
                            return PaymentResult(402)
                    break
                except ProviderUnavailable:
                    if attempt == 1:
                        operation.set_status(Status(StatusCode.ERROR, "provider unavailable"))
                        return PaymentResult(503)
            with self._tracer.start_as_current_span("payment.persist"):
                self._store.save(amount, currency)
            return PaymentResult(201)


def main() -> None:
    provider = TracerProvider(sampler=ALWAYS_ON)
    try:
        application = Payments(
            provider.get_tracer("payments"), SandboxGateway([False]), MemoryStore()
        )
        with Telemetry(provider=provider) as telemetry:
            with telemetry.observe() as observation:
                response = application.submit(1000, "GBP")
            assert response.status_code == 402
            expect(observation.spans.named("payment.authorise")).to(occur_once())
            expect(observation.spans.named("payment.gateway.authorise")).to(occur_once(), fail())
            expect(observation.spans.named("payment.persist")).to(not_occur())
        print("Declined payment: observed authorisation and failure, with no persistence.")
    finally:
        provider.shutdown()  # The application/harness owns the provider.


if __name__ == "__main__":
    main()
