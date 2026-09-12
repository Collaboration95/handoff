from __future__ import annotations

from time import monotonic
from typing import Protocol

from .models import Decision, GateResult, InvoiceDraft, InvoiceRequest
from .rules import validate_invoice
from .telemetry import trace_scope


class Planner(Protocol):
    async def initial(self, request: InvoiceRequest) -> InvoiceDraft: ...

    async def repair(
        self, request: InvoiceRequest, baseline: InvoiceDraft, checks: GateResult
    ) -> list[InvoiceDraft]: ...


def _usage(planner: Planner) -> dict:
    usage = getattr(planner, "usage", {})
    return dict(usage) if isinstance(usage, dict) else {}


async def _improve(request: InvoiceRequest, planner: Planner) -> Decision:
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

    with trace_scope("validate-baseline", baseline, metadata={"request_id": request.request_id}) as span:
        baseline_checks = validate_invoice(request, baseline)
        span.set_output(baseline_checks)
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

    if baseline_checks.review_reasons:
        with trace_scope(
            "stop-on-source-blocker",
            {"review_reasons": baseline_checks.review_reasons},
            metadata={"request_id": request.request_id},
        ) as span:
            span.set_output({"status": "needs_review", "repair_attempted": False})
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

    with trace_scope(
        "validate-candidates",
        [candidate.model_dump(mode="json") for candidate in candidates],
        metadata={"request_id": request.request_id, "candidate_count": len(candidates)},
    ) as span:
        candidate_checks = [validate_invoice(request, candidate) for candidate in candidates]
        span.set_output([checks.model_dump(mode="json") for checks in candidate_checks])
    with trace_scope(
        "select-candidate",
        {"candidate_count": len(candidates)},
        metadata={"request_id": request.request_id},
    ) as span:
        selected = next(
            (candidate for candidate, checks in zip(candidates, candidate_checks) if checks.valid),
            None,
        )
        span.set_output({"selected": selected.model_dump(mode="json") if selected else None})
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


async def improve(request: InvoiceRequest, planner: Planner) -> Decision:
    with trace_scope(
        "improve-invoice-plan",
        request,
        metadata={"request_id": request.request_id},
        version="source-backed-invoice-v1",
    ) as trace:
        decision = await _improve(request, planner)
        trace.set_output(
            {
                "request_id": decision.request_id,
                "status": decision.status,
                "selected": decision.selected,
                "checks": decision.baseline_checks,
            }
        )
    return decision.model_copy(update={"trace_id": trace.trace_id, "trace_url": trace.trace_url})
