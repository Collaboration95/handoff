# Two-minute finance demo

Use two humans in the same hosted LiveKit room, with headphones, and one
Handoff GPT-Live-1 agent. The existing LiveKit clients handle the call. Share
the companion invoice page from the operator's laptop.

## Before the clock starts

- Start the API and voice agent using the launch commands in the README.
- Verify OpenAI, the hosted room, Airwallex **sandbox**, and Langfuse readiness.
- Prepare the synthetic customer/product through the demo setup operation.
- Select the September case. It contains eight accepted units, two previously
  invoiced units, and a signed amendment setting the unit price to SGD90.
- Prefer the voice autofix command below to demonstrate the conversation
  activating the workflow. Use **Simulate autofix** as the manual fallback.
  Both start a fresh September proposal with ten units at SGD100 and
  immediately check and attempt to repair it. This is a controlled test, not
  a measured failure of Airwallex's native agent. Keep its label visible.
- The dedicated autofix tool always selects the controlled fault mode,
  regardless of `FINANCE_FAULT_INJECTION`. That flag is only needed to inject
  the stale proposal in the ordinary voice invoice-proposal path. Leave it
  false when measuring a real model baseline through that ordinary path.
- Ensure there is no previous pending proposal. Rehearsing the same billing
  reference reuses a known verified draft only after a fresh matching readback.
  The page reports **Existing sandbox draft verified**. Say that it verified
  the existing draft; do not describe it as a new invoice. A mismatch or an
  uncertain earlier write remains blocked from making a second invoice.
- Keep the actual Langfuse trace page available. Use the full recorded
  evaluation report for metrics; do not invent a percentage improvement.
- Describe the evidence as platform-supplied synthetic records. We have not
  connected to a customer's delivery or ERP system. Such a connection belongs
  to the stretch goal; the middleware cannot verify unavailable facts.

## On-stage script

| Time | Speaker / action |
| --- | --- |
| 0:00–0:20 | Person 1: “Can we close out the September implementation work? Eight units were accepted, and two were already invoiced.” Person 2: “The signed amendment changed the rate to ninety dollars. Handoff, simulate how you automatically fix an incorrect invoice for the September work. Do not create or send it yet.” |
| 0:20–0:35 | Handoff starts the controlled autofix test; the operator can click **Simulate autofix** as a fallback. The page updates while the real model repairs the injected proposal. No invoice has been created. |
| 0:35–0:55 | Show the original, verified proposal, and actual correction summary. “This controlled stale draft would bill ten units at the old rate. The evidence supports six at SGD90: SGD540 before the supplied tax.” Expand the failed original checks. When the state says **Autofix complete · awaiting approval**, the operator clicks **Approve invoice workflow**. |
| 0:55–1:15 | Handoff calls the approved draft-creation tool (or the operator uses **Create sandbox draft** as a fallback). Show actual activity and invoice ID. Wait for readback verification before describing success. |
| 1:15–1:35 | Handoff announces the actual result in the call. Open the correlated Langfuse trace and point to source evidence, model call, checks, and external operation. |
| 1:35–2:00 | “The platform supplies the objective. Handoff checks the proposed execution, repairs it only if needed, and verifies what the finance platform actually created. This demo uses synthetic evidence and a sandbox draft.” |

If the operator starts the test with the UI button, narrate the correction
manually and use the UI's **Create sandbox draft** button after approval. The
voice agent may still be watching an earlier proposal, so do not promise a
spoken result for this fallback. Use the voice-triggered path when demonstrating
that the conference conversation activates the workflow.

## If a dependency fails

Leave the actual failed/pending status visible. Say which connection failed.
Do not replace a live result with an unlabelled recording, announce a verified
invoice when only creation succeeded, or imply that a draft was sent or paid.
An explicitly labelled recorded run can illustrate the flow while disclosing
that the current run did not complete.

## Complex-command rehearsal

**Simulate autofix** also starts a fresh controlled test when the current action
is **Needs review**. It keeps the business objective correct and injects the
incorrect starting proposal separately. Do not ask the model to preserve
intentional errors: that describes a different objective and can legitimately
result in review. The button starts actual checking and a model repair attempt;
it never displays a prerecorded success or creates an invoice automatically.
If repair cannot produce a supported proposal, **Needs review** remains the
honest outcome. Approval and **Create sandbox draft** follow successful repair.

Use this command for the same small fixture; it requires no additional
integration or model pipeline:

> Create the September implementation invoice for PO-2026-0912. The original
> order was ten units at SGD100, but use the signed September amendment at
> SGD90. Eight units were accepted and two of those were already invoiced.
> Bill only the remaining accepted work, with the supplied 9% tax and
> thirty-day payment terms. Keep the result as a draft.

In controlled mode, the injected upstream proposal incorrectly uses ten units
at SGD100. The source checks identify quantity, unit-price, and contract-ID
errors. The supported result is six units at SGD90: SGD540 subtotal,
SGD48.60 supplied tax, and SGD588.60 total. The objective remains unchanged;
only the execution plan is repaired. These amounts come from the synthetic
fixture, not inferred tax advice.

An optional second scene selects **conflicting latest contracts**. The correct
outcome is a request for review because changing a draft cannot resolve
contradictory signed evidence. Keep this outside the two-minute primary demo.

## Claims this demo supports

The controlled test shows whether the checks catch known invoice errors and
whether bounded repair produces a source-backed plan. Live mode separately
tests actual model outcomes and may correctly report no changes needed.

An empirical advantage over Airwallex's official contract-to-billing agent
requires running that unmodified workflow on the same evidence. Inspecting
its published skill is not such a benchmark. The sandbox integration proves
draft creation and readback only; it does not prove invoice delivery, payment,
or production compliance. Slack and AML remain stretch goals.
