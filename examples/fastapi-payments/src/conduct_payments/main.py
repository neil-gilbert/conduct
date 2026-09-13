"""Run: uvicorn conduct_payments.main:create_app --factory --reload."""

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from pydantic import BaseModel, Field

from .payments import (
    Payment,
    PaymentDeclined,
    Payments,
    PaymentStore,
    SuspendedAccount,
    UnknownAccount,
)


class PaymentRequest(BaseModel):
    account_id: str = Field(min_length=1)
    amount: int = Field(
        gt=0, le=1_000_000, strict=True, description="Amount in minor currency units"
    )
    currency: Literal["GBP", "EUR", "USD"] = "GBP"


def create_app(*, provider: TracerProvider | None = None) -> FastAPI:
    owns_provider = provider is None
    provider = provider if provider is not None else TracerProvider(sampler=ALWAYS_ON)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = PaymentStore()
        app.state.payments = Payments(provider.get_tracer("payments-api"), store)
        try:
            yield
        finally:
            store.close()
            if owns_provider:
                provider.shutdown()

    app = FastAPI(
        title="Conduct payments example",
        description="A local payment sandbox with behavioural tests powered by Conduct.",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/payments", status_code=201, response_model=Payment)
    def submit_payment(request: PaymentRequest) -> Payment:
        try:
            return app.state.payments.submit(request.account_id, request.amount, request.currency)
        except UnknownAccount as error:
            raise HTTPException(404, "Account not found") from error
        except SuspendedAccount as error:
            raise HTTPException(403, "Account is suspended") from error
        except PaymentDeclined as error:
            raise HTTPException(402, "Payment declined: insufficient funds") from error

    @app.get("/payments/{payment_id}", response_model=Payment)
    def get_payment(payment_id: str) -> Payment:
        payment = app.state.payments.get(payment_id)
        if payment is None:
            raise HTTPException(404, "Payment not found")
        return payment

    return app
