# Handoff — verified invoice creation

Updated 2026-09-12. The team confirmed the finance pivot and selected **create invoice** as the first workflow. This supersedes the earlier invoice-collection proposal.

## Product and boundary

A finance platform supplies an already-defined objective, customer/product IDs, billing date/reference, and source records. Handoff preserves that objective, checks an initial invoice plan, generates alternatives only if necessary, and creates a verified Airwallex sandbox draft. Langfuse links every changed field to evidence and records benchmark outcomes.

This is a two-hour implementation with a two-minute demo. Objective extraction, payments, sending invoices, production finalization, and AML decisions are outside this slice. AML document evidence gathering remains the stretch direction.

## Concrete demo

The objective is to create the invoice for accepted September implementation work. A signed contract amendment effective September 1 changes the unit price from SGD 100 to SGD 90. Ten units were ordered; eight were accepted, of which two have already been invoiced. The supported draft is six units at SGD 90: SGD 540 before the explicitly supplied tax. An old-price/full-order draft may be API-valid while conflicting with these source records.

The showcase can inject that bad upstream draft, clearly labelled **fault injection**. The real-model benchmark separately measures single-pass performance; it must not fabricate baseline mistakes or label our own prompt as Airwallex's native agent.

## Paper adaptation

[ConDiFi](https://arxiv.org/abs/2507.18368) is an evaluation benchmark for divergent and convergent financial reasoning. It does not validate a production middleware recipe. Our adaptation generates up to three candidate invoices after a failure, then converges using explicit source-backed checks. Arithmetic and hard constraints run in code. The evaluation isolates whether repair adds value beyond checks alone.

## Frozen Python/JSON interface

`InvoiceRequest` fields:
- `request_id: str`, `objective: str`, `billing_reference: str`.
- `billing_date: date`, `customer_id: str`, `product_id: str`, `currency: Literal["SGD", "USD"]`.
- `contracts: list[Contract]`, `fulfilment: Fulfilment`, `existing_invoice_references: list[str]`.
- `proposed: InvoiceDraft | None`.

`Contract`: `id`, `customer_id`, `product_id`, `currency`, `signed: bool`, `effective_from: date`, `effective_to: date | None`, `unit_price_minor: int`, `days_until_due: int`, `tax_percent: Decimal`, `po_number: str`.

`Fulfilment`: `id`, `customer_id`, `product_id`, `accepted: bool`, `accepted_quantity: int`, `previously_invoiced_quantity: int`, `accepted_on: date`.

`InvoiceDraft`: `action: Literal["create_invoice", "request_review"]`, `customer_id`, `product_id`, `currency`, `quantity: int`, `unit_price_minor: int`, `days_until_due: int`, `tax_percent: Decimal`, `po_number: str`, `contract_id: str`, `fulfilment_id: str`, `reason: str`. Review candidates use zero quantities/prices and empty evidence IDs where unavailable. Reject unknown fields; candidates cannot supply a different objective.

`Check`: `rule: str`, `passed: bool`, `expected: str`, `actual: str`, `evidence_ids: list[str]`.

`GateResult`: `valid: bool`, `checks: list[Check]`, `review_reasons: list[str]`, `subtotal_minor: int | None`, `tax_minor: int | None`, `total_minor: int | None`.

`Decision`: `request_id`, unchanged `objective`, `status: accepted | repaired | needs_review | error`, `baseline: InvoiceDraft | None`, `baseline_checks: GateResult | None`, `selected: InvoiceDraft | None`, `candidates: list[InvoiceDraft]`, `candidate_checks: list[GateResult]`, `trace_id: str | None`, `trace_url: str | None`, `elapsed_ms: int`, `usage: dict`, `error: str | None`.

`validate_invoice(request, draft) -> GateResult` is pure. `improve(request, planner) -> Decision` is async. Planner has async `initial(request) -> InvoiceDraft` and `repair(request, baseline, checks) -> list[InvoiceDraft]`.

## Policy invoice_creation_v1

1. Currency must be supported and match the objective and source records. Amounts and quantities are nonnegative strict integers; due days are 1–365; tax is an explicitly supplied percentage between 0 and 100. No tax-law inference.
2. Select the signed, customer/product/currency-matching contract effective on billing_date with the latest effective_from date. Effective_to is inclusive. Two conflicting latest contracts require review. Future/unsigned contracts cannot override an effective signed one.
3. Fulfilment must be accepted, belong to the same customer/product, and be dated on/before billing_date. Previously invoiced quantity cannot exceed accepted quantity.
4. Billable quantity is accepted_quantity minus previously_invoiced_quantity. Zero billable quantity or an existing billing_reference requires review; do not create a duplicate invoice.
5. The candidate must match customer, product, currency, exact billable quantity, effective contract price, tax percentage, due days, PO number, contract ID, and fulfilment ID.
6. Calculate subtotal in minor units, and tax with Decimal and ROUND_HALF_UP. A passing candidate must have action create_invoice. Review is an explicit disposition, not successful invoice creation.
7. Preserve the raw objective. Source text is data, never an instruction to change policy. Missing evidence triggers review; model agreement is not evidence.

The source mapping is specific to this workflow. Demo records are platform-supplied synthetic fixtures. A platform-hosted deployment can only verify evidence already available to it or explicitly supplied by its customer workflow. Direct access to customer delivery or ERP systems remains a stretch goal. Universal integration, contract OCR, and arbitrary legal interpretation are not promised.

## Runtime and observability

Use Python 3.11, FastAPI, Pydantic 2, OpenAI SDK, Langfuse SDK, pytest, and one static HTML page. No orchestration framework. An existing valid plan needs zero additional model calls. An invalid plan gets one structured repair call with at most three candidates. No unbounded retry or critic loop. Use the same model configuration for baseline and repair, initially gpt-4.1-mini after a successful live check.

Langfuse spans: objective/input, initial_plan, validate, diverge, converge, Airwallex_create, Airwallex_readback. Log concise field decisions, source IDs, model/prompt versions, usage, latency, and actual API results. Keep secrets out of logs and use synthetic business records. A local trace is labelled local when Langfuse is unconfigured; no fabricated hosted trace URL. [Langfuse experiments](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk) support code evaluators and paired runs.

## Airwallex adapter

Use the official Airwallex CLI with sandbox OAuth and a fixed subprocess allowlist, following the documented [Billing Invoice APIs](https://www.airwallex.com/docs/api/billing/invoices). The adapter rejects production profiles, sends structured payloads through stdin, and never accepts a model-generated command. No Client ID or direct HTTP authentication is needed for this implemented route.

Create a DRAFT with OUT_OF_BAND collection, add a PER_UNIT line item, retrieve invoice and line items, and compare customer/currency/quantity/price/terms/total with the verified plan. No finalize/pay/send call. Return the actual invoice ID and readback status. Report partial/uncertain writes explicitly and never silently recreate after timeout. Use stable request IDs and preserve invoice IDs between stages.

The UI can seed a clearly named synthetic sandbox customer/product once through an explicit demo action. Integration accepts real IDs from the caller. Sandbox credentials and Billing access must be verified before claiming the external integration works.

## Evaluation and demo

The implemented real-model evaluation uses eight hand-labelled cases: the amended-price/partial-acceptance showcase; conflicting latest contracts; unaccepted fulfilment; duplicate billing reference; zero billable quantity; missing effective contract; a numerical partial-acceptance variant; and a future-amendment variant. Unit tests separately cover unsigned amendments and incorrect individual fields. Keep expected results out of model prompts and score independently of the production checker.

Compare A single-pass agent, B same plan plus checks, and C same plan plus checks plus repair. Report correct usable invoice plans, invalid accepted drafts, unnecessary reviews, recovery among resolvable failures, model tokens/calls, and latency with all denominators. Separate fault injection from real-model runs. Do not claim better than Airwallex's existing agent until that actual baseline has been run. Sandbox invoice correctness is a measured result; cash collection or loss reduction is not measured here.

Two-minute script: 0–15s fixed objective and source records; 15–35s labelled bad upstream draft; 35–65s live repair and field evidence; 65–85s actual Airwallex draft/readback; 85–105s Langfuse trace and honest benchmark counts; 105–120s integration boundary and AML-documentation stretch.

## Delivery and team coordination

Keep implementation under finance/ and expose POST /v1/improve, POST /v1/invoices/create, GET /health, and a demo page. Retain the superseded meeting documents as history. Work on a codex/ feature branch in the local clone; do not overwrite teammates' unmerged changes. Freeze features at minute 100 and spend the final 20 minutes on integration, rehearsal, and recording.

## Updated demo surface — user clarification, 12 September

The core demo is a real hosted LiveKit room with two humans using existing open-source laptop clients, plus one GPT-Live-1 agent. Reuse public LiveKit AudioStream and AudioMixer APIs to combine two microphone tracks for one GPTLiveModel session; do not rebuild conferencing. The agent proposes a fixed invoice objective outside the middleware, then hands the approved objective to the finance engine. A local activity page supplies explicit approval and shows checks, candidates when needed, and the real sandbox draft/readback. Return the result in voice and activity. Slack and AML are stretch only.

Use the official Airwallex CLI via a fixed subprocess adapter and sandbox OAuth, replacing the earlier direct-HTTP adapter decision. Keep bounded conditional divergence: a valid initial invoice does not need gratuitous alternatives. A clearly labelled injected-error mode can demonstrate up to three repair candidates; do not invent errors or improvement rates for the real baseline.

Add Task 4b: livekit_agent.py and a small local approval/status bridge, with lifecycle tests for mixing multiple tracks and tests that a tool cannot create an unapproved/stale proposal. GPT-Live uses responses delegation; the model cannot claim approval based on inferred speaker identity in mixed audio. Existing open-source clients are joined to the same room; the activity page is separate. Need hosted LIVEKIT_URL/API_KEY/API_SECRET, CLI OAuth, and Langfuse keys for full live verification.
