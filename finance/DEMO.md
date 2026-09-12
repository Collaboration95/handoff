# Two-minute finance demo

Use three humans in the same hosted LiveKit room, with headphones, and one
Handoff GPT-Live-1 agent. The existing LiveKit clients handle the call. Share
the companion invoice page from the operator's laptop.

## Before the clock starts

- Start the API and voice agent using the launch commands in the README.
- Verify OpenAI, the hosted room, Airwallex **sandbox**, and Langfuse readiness.
- Prepare the synthetic customer/product through the demo setup operation.
- Select the September case. It contains eight accepted units, two previously
  invoiced units, and a signed amendment setting the unit price to SGD90.
- For the repair demonstration, explicitly enable **Injected stale draft**.
  This starts from ten units at SGD100 and is a controlled test, not a measured
  failure of Airwallex's native agent. Keep its label visible throughout.
- Ensure there is no previous pending proposal. Do not reuse a completed
  billing reference to make another invoice just for rehearsal.
- Keep the actual Langfuse trace page available. Use the full recorded
  evaluation report for metrics; do not invent a percentage improvement.

## On-stage script

| Time | Speaker / action |
| --- | --- |
| 0:00–0:20 | Person 1: “Can we close out the September implementation work?” Person 2: “Eight units were accepted. Two were already invoiced.” Person 3: “The signed amendment changed the rate to ninety dollars. We still need the invoice.” |
| 0:20–0:35 | Handoff proposes the invoice objective in voice and the activity page. The operator approves checking it. |
| 0:35–0:55 | Show the original and selected invoice. “This controlled stale draft would bill ten units at the old rate. The evidence supports six at SGD90: SGD540 before the supplied tax.” Expand the failed checks and repaired candidate. |
| 0:55–1:15 | Operator approves **Create sandbox draft**. Show actual activity and invoice ID. Wait for readback verification before describing success. |
| 1:15–1:35 | Handoff announces the actual result in the call. Open the correlated Langfuse trace and point to source evidence, model call, checks, and external operation. |
| 1:35–2:00 | “The platform supplies the objective. Handoff checks the proposed execution, repairs it only if needed, and verifies what the finance platform actually created. This demo uses synthetic evidence and a sandbox draft.” |

## If a dependency fails

Leave the actual failed/pending status visible. Say which connection failed.
Do not replace a live result with an unlabelled recording, announce a verified
invoice when only creation succeeded, or imply that a draft was sent or paid.
An explicitly labelled recorded run can illustrate the flow while disclosing
that the current run did not complete.

## Claims this demo supports

The controlled test shows whether the checks catch known invoice errors and
whether bounded repair produces a source-backed plan. Live mode separately
tests actual model outcomes and may correctly report no changes needed.

An empirical advantage over Airwallex's official contract-to-billing agent
requires running that unmodified workflow on the same evidence. Inspecting
its published skill is not such a benchmark. The sandbox integration proves
draft creation and readback only; it does not prove invoice delivery, payment,
or production compliance. Slack and AML remain stretch goals.
