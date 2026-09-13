"""Payment behaviour, a local gateway, and a real in-memory SQLite store."""

import sqlite3
from dataclasses import dataclass
from threading import Lock
from uuid import uuid4

from opentelemetry.trace import Tracer


@dataclass(frozen=True)
class Payment:
    id: str
    account_id: str
    amount: int
    currency: str


class UnknownAccount(Exception):
    pass


class SuspendedAccount(Exception):
    pass


class PaymentDeclined(Exception):
    pass


class PaymentStore:
    def __init__(self) -> None:
        # FastAPI dispatches sync endpoints into worker threads. Serialize this connection's use.
        self._lock = Lock()
        self._connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._connection.execute(
            "CREATE TABLE payments "
            "(id TEXT PRIMARY KEY, account_id TEXT, amount INTEGER, currency TEXT)"
        )

    def save(self, payment: Payment) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO payments VALUES (?, ?, ?, ?)",
                (payment.id, payment.account_id, payment.amount, payment.currency),
            )

    def get(self, payment_id: str) -> Payment | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, account_id, amount, currency FROM payments WHERE id = ?", (payment_id,)
            ).fetchone()
        return Payment(*row) if row else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class SandboxGateway:
    """Predictable demo policy: amounts above 10,000 minor units are declined."""

    def authorise(self, amount: int, currency: str) -> None:
        if amount > 10_000:
            raise PaymentDeclined("insufficient_funds")


class Payments:
    def __init__(self, tracer: Tracer, store: PaymentStore) -> None:
        self._tracer = tracer
        self._store = store
        self._gateway = SandboxGateway()
        self._accounts = {"active-account": "active", "suspended-account": "suspended"}

    def submit(self, account_id: str, amount: int, currency: str) -> Payment:
        attributes = {
            "account.id": account_id,
            "payment.amount": amount,
            "payment.currency": currency,
        }
        with self._tracer.start_as_current_span(
            "payment.authorise", attributes=attributes
        ) as action:
            with self._tracer.start_as_current_span("customer.lookup"):
                state = self._accounts.get(account_id)
                if state is None:
                    raise UnknownAccount(account_id)
            if state == "suspended":
                action.add_event("account.suspended")
                raise SuspendedAccount(account_id)
            with self._tracer.start_as_current_span(
                "payment.gateway.authorise", attributes=attributes
            ) as request:
                try:
                    self._gateway.authorise(amount, currency)
                except PaymentDeclined:
                    request.add_event("payment.declined", {"reason": "insufficient_funds"})
                    raise
            payment = Payment(uuid4().hex, account_id, amount, currency)
            with self._tracer.start_as_current_span("payment.persist") as persistence:
                self._store.save(payment)
                persistence.set_attribute("payment.id", payment.id)
            return payment

    def get(self, payment_id: str) -> Payment | None:
        with self._tracer.start_as_current_span("payment.lookup"):
            return self._store.get(payment_id)
