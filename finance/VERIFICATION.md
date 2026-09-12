# Demo verification — 12 September 2026

Handoff's demo uses two human participants, GPT-Live-1, an existing LiveKit client, and the finance approval page. Its descriptor is **Financial actions, verified.**

## Verified integrations

- **Invoice repair:** an explicitly injected proposal for ten units at SGD100 was repaired to six units at SGD90 using supplied synthetic evidence. Subtotal SGD540; supplied tax SGD48.60; total SGD588.60. The objective was preserved.
- **Airwallex sandbox:** the browser created draft `inv_sgpvpmn67hm7t4jjv0e` for the unique rehearsal reference `HANDOFF-REHEARSAL-20260912T0643Z`. All 13 external readback checks passed. No finalization, payment, or customer delivery was performed.
- **Langfuse:** [the creation trace](https://us.cloud.langfuse.com/project/cmtxxe6ur06dhad0i1aq2s59j/traces/a618c6935e359a0a195631bd49cbf983) was read back through the installed SDK. It contains the root/session correlation and actual invoice-create, line-add, invoice-get, and line-list tool observations, with inputs and outputs. [The repair trace](https://us.cloud.langfuse.com/project/cmtxxe6ur06dhad0i1aq2s59j/traces/101ec52f1461668e416b8694e4747b8b) records the corresponding model decision.
- **LiveKit and GPT-Live:** two synthetic remote microphone publishers joined the Cloud room. The bridge reported two microphones, GPT-Live heard the mixed audio, and its delegate called the real proposal API. The resulting proposal remained pending and unapproved, with no invoice. A separate synthetic listener received nonzero audio published by the agent. Both test clients were disconnected afterward.
- **React starter compatibility:** its token route must join the fixed `handoff-finance` room. The bridge's token must use participant kind `agent` and allow updating its own metadata, so the starter can recognize `lk.agent.state=listening`. These settings were verified against LiveKit RoomService. A default starter with random rooms cannot reach this already-running bridge.

The two-person rehearsal with actual laptop microphones remains an operator check for speech clarity, echo, and timing. Synthetic media tests are not a claim of production reliability.

## Automated checks

```bash
PYTHONPATH=finance .venv/bin/python -m pytest -q -o addopts='' finance/tests
```

94 tests passed. The only warning was an upstream Starlette/AnyIO deprecation. Independent review findings on proposal races, microphone lifecycle, paired latency, and selected-candidate display were fixed and reviewed again.

## Evaluation results and limits

The latest eight-case real-model run used eight model calls and 5,514 tokens. Original, checked-only, and repaired variants tied on all eight cases: each had 7/8 correct dispositions and 3/3 usable invoices on resolvable cases. One missing-contract case failed to produce a valid structured baseline. There is no measured accuracy advantage over that baseline in this run.

The separate controlled-fault case recovered the exact supported invoice in one repair call, using 1,507 tokens. This demonstrates recovery from an injected error, not a failure observed in Airwallex's native agent. That native agent has not been benchmarked here.

Historical reports under ignored `finance/runs/` retain their original data. Reports generated before the paired-latency fix undercount repaired per-variant latency; their overall `actual_execution` timing remains valid. New evaluations include the shared baseline time in each variant.

## Delivery boundary

Handoff creates the corrected draft in the merchant's Airwallex sandbox account and returns its ID and verified status to the page/API. It never creates the intentionally incorrect proposal in Airwallex. It does not send either proposal to a customer. Finalization and delivery to a customer's billing email are separate operations outside this demo.

Source records are platform-supplied synthetic fixtures. Direct customer-system access, Slack delivery, and AML evidence gathering remain stretch goals. Credentials, room tokens, CLI binaries, and raw local run files are excluded from Git.
