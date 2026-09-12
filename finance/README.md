# Handoff finance demo

This local demo checks a proposed invoice against synthetic contract and fulfilment records, records exact human workflow approval, creates an Airwallex **sandbox draft**, and reports success only after readback verification. The existing LiveKit clients handle the meeting; the page on port 8000 is the approval and activity companion.

## Install and configure

Use Python 3.11 or newer from the repository root:

```bash
python -m venv .venv
.venv/bin/pip install -e ./finance
cp finance/.env.example .env.local
```

Fill the needed placeholders in the root `.env.local`; the app reads no browser-supplied credentials and never returns secret values. OpenAI enables live baseline or repair calls. Langfuse needs both keys for hosted traces. The voice bridge uses `LIVEKIT_URL` plus a participant `LIVEKIT_TOKEN`; a current heartbeat is still required before the UI reports it connected.

Install the official [Airwallex CLI](https://github.com/airwallex/airwallex-cli), put `airwallex` on `PATH` or set `AIRWALLEX_CLI_PATH`, then sign into sandbox mode:

```bash
curl -fsSL https://static.airwallex.com/developer-tools/airwallex-cli/install.sh | sh
airwallex auth login
airwallex auth whoami --compact --no-telemetry
```

Do not use `--prod`; the adapter rejects any authenticated production profile. If this repository includes `.tools/airwallex`, it is the final local fallback after `AIRWALLEX_CLI_PATH` and `PATH`.

## Launch

Start the approval page and API from the repository root:

```bash
PYTHONPATH=finance .venv/bin/python -m uvicorn handoff_finance.app:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The page creates one pending proposal from the selected fixture. **Approve invoice workflow** records human approval and runs the checks. A ready proposal can be created by the voice agent or by the page’s **Create sandbox draft** fallback. That second button is not another approval.

Before the voice proposal, `POST /v1/demo/setup` can create or read back the clearly named synthetic Airwallex customer and product. The returned mapping is applied consistently to later fixture proposals in this server process. This setup creates no invoice and sends no customer message.

## Locked API contract

All JSON inputs reject unknown fields.

- `POST /v1/proposals` accepts `{objective: string, case_name: string = "showcase", fault_injection: boolean = false}` and returns the current proposal. The objective is stored unchanged. The backend assigns the ID and snapshots the selected fixture; callers cannot supply finance fields or URLs.
- `GET /v1/proposals/current` returns the current proposal or `null`. `GET /v1/proposals/{id}` returns one known proposal or 404.
- `POST /v1/proposals/{id}/approve` records human workflow approval and runs checks or one bounded repair. It returns the updated proposal. Voice tools must never call this endpoint.
- `POST /v1/proposals/{id}/cancel` cancels the current pending, ready, or review proposal. Creating a fresh proposal cancels any earlier nonterminal proposal, so its approval cannot carry forward.
- `POST /v1/invoices/create` accepts only `{proposal_id: string}`. It requires the current server-owned, human-approved, verified selection; revalidates the stored request and draft; and calls the journaled Airwallex adapter. Repeated calls return the current terminal result. A known successful, partial, or uncertain write reserves its billing reference across later proposal IDs.
- `POST /v1/improve` accepts the strict full `InvoiceRequest` contract for direct platform demos, stores a new immutable server-owned proposal/decision ID, marks that direct workflow approved, and returns the proposal.
- `GET /v1/activity` returns `{current_status, current_proposal_id, events}`. Events contain IDs, proposal IDs, types, safe messages, and UTC timestamps.
- `POST /v1/voice/status` accepts `{connected, room_name, human_microphones, model, error}`. `GET /health` and `GET /v1/health` mark this heartbeat stale and disconnected after 15 seconds.
- `GET /v1/cases` lists the synthetic fixtures. `GET /v1/evaluation/latest` returns the latest recorded report or an explicit `{available:false, report:null, error}` empty state.
- `POST /v1/demo/setup` verifies or creates the synthetic sandbox customer and product and returns their IDs.

A proposal has `{id, status, objective, case_name, fault_injection, approved, decision, invoice, error}` plus its immutable request snapshot and timestamps. Status is one of `pending`, `checking`, `ready`, `needs_review`, `creating`, `completed`, `failed`, or `cancelled`. Only `invoice.verified === true` produces `completed`.

## Two-minute demo

1. Confirm the service row reports current readiness. Keep **Injected stale draft** visible for the controlled repair path; leave it off for a real model baseline.
2. Show eight accepted units, two previously billed, and the signed amendment at SGD 90. Approve the workflow.
3. Compare the original draft with the selected six-unit invoice and expand the failed source checks.
4. Create the sandbox draft from voice or the page. Wait for **Draft created and readback verified** before announcing success; otherwise describe the exact partial or failed stage shown.
5. Open the trace link only when the decision contains a real hosted URL. Use the recorded evaluation panel only when an actual report is available. Never present injected faults as an empirical Airwallex failure.

The demo creates drafts only. It does not finalize, send, pay, or charge an invoice. The local server stores proposal state in memory, so restarting it clears approval and activity. The Airwallex adapter journal remains the cross-restart duplicate-write boundary for a request ID.

## Tests

```bash
PYTHONPATH=finance .venv/bin/pytest finance/tests -q
```
