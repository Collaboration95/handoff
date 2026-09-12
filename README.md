# Handoff — verified invoice creation

Handoff accepts a finance platform’s existing objective, improves the proposed invoice against contract and fulfilment records, and traces the result through Langfuse. The core demo creates an Airwallex sandbox draft and checks the readback. AML documentation is the stretch direction.

- [Current design and two-minute demo](docs/superpowers/specs/2026-09-12-finance-outcomes-design.md)
- [Two-hour implementation plan](docs/superpowers/plans/2026-09-12-finance-outcomes.md)
- [Install and run the finance API and approval page](finance/README.md)
- [Connect the GPT-Live-1 participant and two meeting clients](finance/LIVEKIT.md)
- [Two-minute stage script](finance/DEMO.md)
- [Verified integrations, evaluation results, and limits](finance/VERIFICATION.md)

The implementation lives in `finance/`. It includes source-backed invoice checks, conditional model repair, an approval page, the official Airwallex CLI adapter, a LiveKit voice bridge, and Langfuse traces with paired evaluation. The numbered Handoff V2 documents under `docs/` describe the earlier meeting assistant and are superseded.

The controlled demo repairs an injected old-price/full-order draft using a signed amendment, accepted delivery, and prior billing. This demonstrates recovery from a known error; it is not a benchmark of Airwallex's native agent. All evidence is synthetic and external writes create sandbox drafts only.
