# FastAPI payments, tested with Conduct

A small, independently installable FastAPI project demonstrating behavioural testing through HTTP. The app uses a local payment-provider sandbox and an in-memory SQLite database. Its tests use the Conduct library from this GitHub repository, pinned to a specific commit, rather than an unrelated PyPI package.

The application imports OpenTelemetry for instrumentation. **Only the tests import Conduct.** No mock call assertions or library-specific instrumentation helpers are needed.

## Install and test

From the repository root, using Python 3.11 or newer:

```sh
cd examples/fastapi-payments
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
```

This installs FastAPI, Uvicorn, HTTPX, and the pinned GitHub version of Conduct. To test local library changes instead, install the root project afterwards with `python -m pip install -e ../..`.

## Run the API

```sh
uvicorn conduct_payments.main:create_app --factory --reload
```

Open [interactive API docs](http://127.0.0.1:8000/docs), or submit a payment:

```sh
curl -X POST http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"active-account","amount":1000,"currency":"GBP"}'
```

`GET /payments/{id}` retrieves the payment returned by a successful request. `GET /health` reports readiness.

| Request | Response | Behaviour asserted by Conduct |
| --- | --- | --- |
| Active account, amount ≤ 10,000 | 201 | Authorisation then persistence, in the same workflow |
| Active account, amount > 10,000 | 402 | Provider declined; no persistence |
| `suspended-account` | 403 | No provider request and no persistence |
| Unknown account | 404 | Lookup failed; no provider request |
| Invalid amount or currency | 422 | Payment workflow never began |

Amounts are positive integers in minor currency units. Supported currencies are GBP, EUR, and USD. The gateway rule is a deterministic demo policy, with no external payments or credentials. The database is local to the app instance and disappears on shutdown.

## What a test looks like

Setup supplies the same `TracerProvider(sampler=ALWAYS_ON)` to the app and `Telemetry`, then creates an HTTPX client. The assertion itself is small:

```python
with telemetry.observe() as observation:
    response = await client.post(
        "/payments",
        json={"account_id": "suspended-account", "amount": 1000},
    )

assert response.status_code == 403
expect(observation.spans.named("payment.gateway.authorise")).to(not_occur())
expect(observation.spans.named("payment.persist")).to(not_occur())
```

The complete [test suite](tests/test_api.py) also checks concurrent request isolation, attributes, events, status, parent/descendant structure, temporal order, and a deliberately incorrect expectation's diagnostics.

Tests use HTTPX `AsyncClient` with `ASGITransport` to exercise real FastAPI routing, validation, worker-thread dispatch, and SQLite access **in-process**. They explicitly enter the application's lifespan because HTTPX does not start it automatically. This follows the [FastAPI async-testing pattern](https://fastapi.tiangolo.com/advanced/async-tests/) while using standard-library `unittest` as the host framework.

The separately running Uvicorn server is for manual exploration. Conduct's V1 observation context does not cross a network connection to a separate process.
