# Handoff Invoice Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working, verifiable Airwallex invoice-creation demo in two hours.
**Architecture:** Preserve the incoming objective, validate source-backed invoice fields, repair once if necessary, create a sandbox draft, and read it back. Compare single-pass, checks-only, and repaired outcomes with Langfuse traces.
**Tech Stack:** Python 3.11, FastAPI, Pydantic 2, OpenAI SDK, Langfuse SDK, httpx, pytest, static HTML.
**Spec:** [Invoice creation design](../specs/2026-09-12-finance-outcomes-design.md).

## Global Constraints
- Objective definition and distillation stay with the platform.
- The delivery budget is 120 minutes, including integration and rehearsal; the demo is 120 seconds.
- Create invoice is the core workflow. AML documentation is stretch.
- At most one repair call and three candidate invoices.
- The actual Airwallex agent must be tested before claiming an improvement over it.
- No production charges, sending invoices, or finalization in the core demo.
- No credentials in code, logs, commits, traces, or browser payloads.

## Task 1 — Core contract, checks, and bounded repair

Files: finance/handoff_finance/{__init__,models,rules,engine,model_client,fixtures}.py; finance/tests/test_core.py; finance/pyproject.toml.
Interfaces: use the exact model fields and function signatures in the spec.

- [ ] Write hand-derived tests first: six billable units at SGD90 gives subtotal 54000 minor units; an old SGD100 contract fails. Wrong customer, PO, terms, currency, quantity, and evidence IDs must fail. Unsigned/future amendments cannot win; conflicting latest contracts and unaccepted work require review.
- [ ] Run tests and observe the missing behavior before implementation.
- [ ] Implement pure validation and a structured-output model client using the configured model. Preserve objective text exactly. Reject malformed candidates and perform no extra repair when the initial plan passes.
- [ ] Add engine tests with a fake model boundary: one invalid and one valid candidate; all-invalid candidates; provider exception; malformed model output; objective preservation; candidate limit.
- [ ] Run the core tests and one live model case. Report test command, results, changed files, and any concerns.

## Task 2 — Airwallex draft adapter

Files: finance/handoff_finance/airwallex.py; finance/tests/test_airwallex.py.
Interfaces: async create_verified_invoice(request, draft) -> dict with actual invoice ID, state, stage, and verification checks; async ensure_demo_objects() -> customer_id/product_id mapping. Credentials come from .env.local only on the server.

- [ ] Confirm create/add-line-items/retrieve schemas from official docs; record the exact payloads in tests. No endpoint guessing.
- [ ] Test the HTTP boundary using httpx.MockTransport, asserting actual URLs, field values, Decimal conversion, and readback verification. Test create success followed by add-item failure and a timeout; never claim full success or silently recreate.
- [ ] Implement sandbox authentication using Client ID plus API key, create DRAFT/OUT_OF_BAND, add PER_UNIT pricing, and read back. Use fixed operation request IDs and retain partial invoice IDs. No finalize, pay, or send endpoint.
- [ ] Verify live only when Client ID and sandbox account are confirmed. Surface permission/IP/Billing errors distinctly without secret-bearing response headers.

## Task 3 — Langfuse and paired evaluation

Files: finance/handoff_finance/telemetry.py, evaluate.py; finance/tests/test_evaluation.py.
Interfaces: trace_scope(name, input) and score_case(case, decision); evaluation consumes the same baseline draft across all variants.

- [ ] Trace baseline, checks, candidates, convergence, and external readback with correlated request/case IDs. Add an explicit local-only status if credentials are absent.
- [ ] Implement hand-labelled fixtures and independent scoring. A review on a resolvable case is not a successful invoice. Report all cases and separate fault injection from real-model cases.
- [ ] Test scores on literal expected plans. Run the suite with real model output and retain actual results, including ties and regressions.
- [ ] Verify one actual Langfuse trace and its linked metrics before claiming hosted auditing works.

## Task 4 — API and demo page

Files: finance/handoff_finance/app.py, finance/static/index.html, finance/tests/test_api.py, finance/README.md.
Interfaces: POST /v1/improve, POST /v1/invoices/create, GET /health, GET /.

- [ ] Write API tests for objective preservation, invalid input, unconfigured provider, and rejecting unverified invoice writes.
- [ ] Serve one page showing source records, original and selected invoice, field checks, real external invoice status, trace link, tokens, and latency. Clearly label injected faults, local traces, and recorded results.
- [ ] The create endpoint revalidates the plan server-side before Airwallex access. Browser never receives credentials.
- [ ] Run all tests, start the server, exercise a real request through the UI, and inspect the Airwallex readback and Langfuse trace when configured.

## Task 5 — Integration and recording

- [ ] Fetch teammates' changes and integrate through one owner; never force-push or overwrite shared work.
- [ ] Freeze at minute 100. Rehearse the exact script in the spec twice and record a 120-second demo. Mark any replay as replay.
- [ ] Document the tested commit, launch command, actual integrations verified, and remaining limitations.

## Time budget
0–10: configuration and contract. 10–40: checks and engine. 40–65: Airwallex adapter and live draft. 65–85: observability and evaluation. 85–100: API/UI integration. 100–120: fixes, rehearsal, recording. Credential-independent checks and UI can proceed while external account access is pending.
