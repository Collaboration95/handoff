# Task 4 report — API and approval page

Date: 2026-09-12

## Outcome

Implemented a localhost FastAPI application and one responsive static approval/activity page. The browser can select a synthetic evidence case, visibly enable controlled fault injection, approve the exact current workflow, inspect baseline-versus-selected invoice fields and checks, and use a manual sandbox-create fallback. It never submits invoice fields, integration URLs, credentials, or approval assertions to the create endpoint.

The server owns proposal IDs and immutable request snapshots. A fresh proposal cancels any earlier nonterminal proposal. Approval runs the existing checker and at most one bounded repair. Creation requires the current approved proposal, parses and revalidates the stored selected draft, uses the Airwallex adapter's lock/journal, and marks `completed` only when `invoice.verified` is exactly `true`. Successful, partial, and uncertain adapter results also reserve the billing reference across later proposal IDs in the process.

A proposal cannot be replaced while its checks or external creation are in progress; the API returns `409 proposal_in_progress` before mutating state. Two held-await concurrency tests cover both phases. During those phases the UI also disables the case and fault-injection selectors. For repaired decisions the field-check panel resolves the gate belonging to the selected candidate, while the original invoice remains explicitly labelled as an injected controlled test when that mode is active.

## API contract

- `POST /v1/proposals` accepts `{objective, case_name="showcase", fault_injection=false}` and returns a Proposal.
- `GET /v1/proposals/current` returns `Proposal | null`; `GET /v1/proposals/{id}` returns a known proposal or 404.
- `POST /v1/proposals/{id}/approve` records workflow approval and runs checking/repair. Voice tools do not use this route.
- `POST /v1/proposals/{id}/cancel` cancels the current pending, ready, or review proposal.
- `POST /v1/invoices/create` accepts only `{proposal_id}` and returns the updated Proposal.
- `POST /v1/improve` accepts a strict full `InvoiceRequest`, stores a new server-owned proposal/decision ID, and returns the Proposal.
- `GET /v1/activity` returns recent state events and current status.
- `POST /v1/voice/status` accepts `{connected, room_name, human_microphones, model, error}`. `GET /health` and `GET /v1/health` mark a heartbeat older than 15 seconds stale and disconnected.
- `GET /v1/cases` returns the synthetic fixture catalog. `GET /v1/evaluation/latest` returns the latest real recorded report or an explicit empty state.
- `POST /v1/demo/setup` verifies or creates the named synthetic Airwallex customer and product. Its mapping is consistently applied to subsequent fixture proposals in the process.

Proposal states are `pending`, `checking`, `ready`, `needs_review`, `creating`, `completed`, `failed`, and `cancelled`. The locked required fields are `id`, `status`, `objective`, `case_name`, `fault_injection`, `approved`, `decision`, `invoice`, and `error`; the response also contains the request snapshot and timestamps.

## Observability

The existing improve engine supplies the decision trace ID/URL. Invoice creation uses a parent `create-verified-invoice` trace and child `create-airwallex-draft` and `readback-airwallex-draft` spans. The proposal ID is the Langfuse session ID, and the parent metadata includes the decision trace ID. The invoice response contains the creation trace status/ID/URL so the page can link the external operation separately. Telemetry sanitization masks secret-like keys and signed URL queries.

The live read-only service check reported OpenAI ready, Airwallex authenticated in sandbox, Langfuse ready, and LiveKit credentials configured. LiveKit remained correctly disconnected/stale until an actual voice heartbeat arrived. The evaluation endpoint returned the latest real-model report with 8 cases and explicit numerators/denominators; the UI does not invent or hard-code those metrics.

Root separately verified the Airwallex adapter with a unique smoke billing reference. Invoice `inv_sgpvpmn67hm7s9pee19` passed all 13 readback checks; the retained receipt is `finance/runs/airwallex-live-smoke.json`. Task 4 did not create another invoice during page verification.

## Verification

TDD red state:

```text
PYTHONPATH=finance .venv/bin/pytest finance/tests/test_api.py -q
ModuleNotFoundError: No module named 'handoff_finance.app'
```

Focused API result after implementation:

```text
PYTHONPATH=finance .venv/bin/pytest finance/tests/test_api.py -q
15 passed
```

Full finance suite after integration with tracing and voice:

```text
PYTHONPATH=finance .venv/bin/pytest finance/tests -q
85 passed, 1 Starlette deprecation warning
```

Additional checks:

```text
PYTHONPATH=finance .venv/bin/python -m compileall -q finance/handoff_finance finance/tests
exit 0

git diff --check -- finance/handoff_finance/app.py finance/static/index.html finance/tests/test_api.py finance/README.md finance/.env.example finance/pyproject.toml
exit 0
```

The exact documented launch command started successfully on `127.0.0.1:8000`. Browser inspection verified the service row, source evidence, invoice comparison, approval/create/cancel controls, field-check and run-evidence empty states, activity timeline, semantic region labels, and keyboard-addressable controls. The desktop screenshot showed the current approval without scrolling and no visual overlap. Browser loading produced only a favicon 404, which was removed by adding an inline empty favicon.

## Launch

```bash
PYTHONPATH=finance .venv/bin/python -m uvicorn handoff_finance.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Installation, `.env.local` placement, official Airwallex CLI setup/login, two-minute flow, exact endpoint bodies, and operational boundaries are documented in `finance/README.md` and `finance/.env.example`.

## Remaining live condition

The page and API are ready for the shared rehearsal. A LiveKit-connected claim still depends on the voice bridge posting a current heartbeat from the hosted room. The app deliberately reports stale/disconnected without one. Proposal state is in memory for one local demo process; the Airwallex adapter journal remains the durable duplicate-write boundary for the same request ID across restarts.
