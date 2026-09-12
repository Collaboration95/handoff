from __future__ import annotations

from time import monotonic
from typing import Protocol

from .models import Decision, GateResult, InvoiceDraft, InvoiceRequest
from .rules import validate_invoice


class Planner(Protocol):
    async def initial(self, request: InvoiceRequest) -> InvoiceDraft: ...

    async def repair(
        self, request: InvoiceRequest, baseline: InvoiceDraft, checks: GateResult
    ) -> list[InvoiceDraft]: ...


def _usage(planner: Planner) -> dict:
    usage = getattr(planner, "usage", {})
    return dict(usage) if isinstance(usage, dict) else {}


async def improve(request: InvoiceRequest, planner: Planner) -> Decision:
    started = monotonic()

    if request.proposed is not None:
        baseline = request.proposed
    else:
        try:
            baseline = await planner.initial(request)
        except Exception:
            return Decision(
                request_id=request.request_id,
                objective=request.objective,
                status="error",
                baseline=None,
                baseline_checks=None,
                selected=None,
                candidates=[],
                candidate_checks=[],
                elapsed_ms=int((monotonic() - started) * 1000),
                usage=_usage(planner),
                error="initial provider failed",
            )

    baseline_checks = validate_invoice(request, baseline)
    if baseline_checks.valid:
        return Decision(
            request_id=request.request_id,
            objective=request.objective,
            status="accepted",
            baseline=baseline,
            baseline_checks=baseline_checks,
            selected=baseline,
            candidates=[],
            candidate_checks=[],
            elapsed_ms=int((monotonic() - started) * 1000),
            usage=_usage(planner),
            error=None,
        )

    try:
        candidates = (await planner.repair(request, baseline, baseline_checks))[:3]
    except Exception:
        return Decision(
            request_id=request.request_id,
            objective=request.objective,
            status="needs_review",
            baseline=baseline,
            baseline_checks=baseline_checks,
            selected=None,
            candidates=[],
            candidate_checks=[],
            elapsed_ms=int((monotonic() - started) * 1000),
            usage=_usage(planner),
            error="repair provider failed",
        )

    candidate_checks = [validate_invoice(request, candidate) for candidate in candidates]
    selected = next(
        (candidate for candidate, checks in zip(candidates, candidate_checks) if checks.valid),
        None,
    )
    return Decision(
        request_id=request.request_id,
        objective=request.objective,
        status="repaired" if selected else "needs_review",
        baseline=baseline,
        baseline_checks=baseline_checks,
        selected=selected,
        candidates=candidates,
        candidate_checks=candidate_checks,
        elapsed_ms=int((monotonic() - started) * 1000),
        usage=_usage(planner),
        error=None,
    )
