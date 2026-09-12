from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Literal

from .engine import improve
from .fixtures import build_case, cases
from .model_client import OpenAIPlanner
from .models import Decision, GateResult, InvoiceDraft, InvoiceRequest
from .rules import validate_invoice
from .telemetry import flush_traces, trace_scope


PROMPT_VERSION = "finance-planner-v1"
POLICY_VERSION = "source-backed-invoice-v1"
_FINANCE_FIELDS = (
    "action",
    "customer_id",
    "product_id",
    "currency",
    "quantity",
    "unit_price_minor",
    "days_until_due",
    "tax_percent",
    "po_number",
    "contract_id",
    "fulfilment_id",
)


@dataclass(frozen=True)
class EvaluationCase:
    name: str
    request: InvoiceRequest
    expected_disposition: Literal["create_invoice", "request_review"]
    expected_invoice: InvoiceDraft | None
    group: Literal["real_model", "fault_injection"]


def _same_invoice(actual: InvoiceDraft | None, expected: InvoiceDraft | None) -> bool:
    if actual is None or expected is None:
        return False
    return all(getattr(actual, field) == getattr(expected, field) for field in _FINANCE_FIELDS)


def score_case(case: EvaluationCase, decision: Decision) -> dict[str, bool | None]:
    """Score against hand-labelled outcomes without using the production validator."""
    resolvable = case.expected_disposition == "create_invoice"
    accepted = decision.selected is not None and decision.selected.action == "create_invoice"
    exact_invoice = resolvable and _same_invoice(decision.selected, case.expected_invoice)
    correct_review = not resolvable and decision.selected is None and decision.status == "needs_review"
    return {
        "correct_disposition": bool(exact_invoice or correct_review),
        "usable_invoice_plan": bool(exact_invoice) if resolvable else None,
        "invalid_accepted_draft": bool(accepted and not exact_invoice),
        "needless_review": bool(resolvable and not accepted),
    }


def _expected_showcase(*, quantity: int = 6) -> InvoiceDraft:
    return InvoiceDraft(
        action="create_invoice",
        customer_id="customer-demo",
        product_id="implementation",
        currency="SGD",
        quantity=quantity,
        unit_price_minor=9_000,
        days_until_due=30,
        tax_percent=Decimal("9"),
        po_number="PO-2026-0912",
        contract_id="contract-sep",
        fulfilment_id="fulfilment-sep",
        reason="Hand-labelled expected finance fields.",
    )


def evaluation_cases(*, fault_injection: bool = False) -> list[EvaluationCase]:
    if fault_injection:
        request = build_case("showcase")
        return [
            EvaluationCase(
                name="showcase_injected_old_price_full_order",
                request=request,
                expected_disposition="create_invoice",
                expected_invoice=_expected_showcase(),
                group="fault_injection",
            )
        ]

    catalog = cases()
    result = [
        EvaluationCase(
            name="showcase",
            request=catalog["showcase"].model_copy(update={"proposed": None}),
            expected_disposition="create_invoice",
            expected_invoice=_expected_showcase(),
            group="real_model",
        )
    ]
    for name in (
        "conflicting_latest_contracts",
        "unaccepted_fulfilment",
        "duplicate_billing_reference",
        "zero_billable_quantity",
        "missing_effective_contract",
    ):
        result.append(
            EvaluationCase(
                name=name,
                request=catalog[name].model_copy(update={"proposed": None}),
                expected_disposition="request_review",
                expected_invoice=None,
                group="real_model",
            )
        )

    partial = build_case("showcase")
    partial = partial.model_copy(
        update={
            "request_id": "partial_acceptance_three_units",
            "proposed": None,
            "fulfilment": partial.fulfilment.model_copy(
                update={"accepted_quantity": 5, "previously_invoiced_quantity": 2}
            ),
        }
    )
    future = build_case("showcase")
    future_contract = future.contracts[-1].model_copy(
        update={
            "id": "contract-future",
            "effective_from": future.billing_date.replace(month=10),
            "unit_price_minor": 7_000,
        }
    )
    future = future.model_copy(
        update={
            "request_id": "future_amendment_ignored",
            "proposed": None,
            "contracts": [*future.contracts, future_contract],
        }
    )
    result.extend(
        [
            EvaluationCase(
                name="partial_acceptance_three_units",
                request=partial,
                expected_disposition="create_invoice",
                expected_invoice=_expected_showcase(quantity=3),
                group="real_model",
            ),
            EvaluationCase(
                name="future_amendment_ignored",
                request=future,
                expected_disposition="create_invoice",
                expected_invoice=_expected_showcase(),
                group="real_model",
            ),
        ]
    )
    return result


def _decision_from_baseline(
    request: InvoiceRequest,
    baseline: InvoiceDraft,
    *,
    checked: GateResult | None,
    usage: dict,
) -> Decision:
    accepted = checked.valid if checked is not None else baseline.action == "create_invoice"
    selected = baseline if accepted else None
    return Decision(
        request_id=request.request_id,
        objective=request.objective,
        status="accepted" if selected is not None else "needs_review",
        baseline=baseline,
        baseline_checks=checked,
        selected=selected,
        candidates=[],
        candidate_checks=[],
        elapsed_ms=0,
        usage=dict(usage),
        error=None,
    )


def _failed_decision(request: InvoiceRequest, usage: dict) -> Decision:
    return Decision(
        request_id=request.request_id,
        objective=request.objective,
        status="error",
        baseline=None,
        baseline_checks=None,
        selected=None,
        candidates=[],
        candidate_checks=[],
        elapsed_ms=0,
        usage=dict(usage),
        error="initial provider failed",
    )


async def _run_case(case: EvaluationCase, *, model: str) -> dict:
    case_started = monotonic()
    planner = OpenAIPlanner(model=model)
    trace_input = {
        "request_id": case.request.request_id,
        "objective": case.request.objective,
        "source_records": case.request.model_dump(mode="json", exclude={"proposed"}),
        "group": case.group,
    }
    with trace_scope(
        "evaluate-invoice-case",
        trace_input,
        metadata={"request_id": case.request.request_id, "case": case.name, "group": case.group},
        version=POLICY_VERSION,
    ) as trace:
        try:
            baseline_started = monotonic()
            if case.group == "fault_injection":
                baseline = case.request.proposed
                assert baseline is not None
            else:
                baseline = await planner.initial(case.request)
            baseline_elapsed_ms = int((monotonic() - baseline_started) * 1000)
        except Exception:
            failed = _failed_decision(case.request, planner.usage)
            variants = {name: failed for name in ("original", "checked_only", "repaired")}
        else:
            baseline_usage = dict(planner.usage)
            original = _decision_from_baseline(case.request, baseline, checked=None, usage=baseline_usage)
            original = original.model_copy(update={"elapsed_ms": baseline_elapsed_ms})
            checks = validate_invoice(case.request, baseline)
            checked = _decision_from_baseline(case.request, baseline, checked=checks, usage=baseline_usage)
            checked = checked.model_copy(update={"elapsed_ms": baseline_elapsed_ms})
            repaired = await improve(case.request.model_copy(update={"proposed": baseline}), planner)
            variants = {"original": original, "checked_only": checked, "repaired": repaired}

        scores = {name: score_case(case, decision) for name, decision in variants.items()}
        for variant, values in scores.items():
            for metric, value in values.items():
                if value is not None:
                    trace.score(f"{variant}_{metric}", value)
        trace.set_output(
            {
                "request_id": case.request.request_id,
                "variant_statuses": {name: decision.status for name, decision in variants.items()},
                "scores": scores,
            }
        )

    return {
        "case": case.name,
        "group": case.group,
        "expected": {
            "disposition": case.expected_disposition,
            "invoice": case.expected_invoice.model_dump(mode="json") if case.expected_invoice else None,
        },
        "trace": {
            "status": trace.status,
            "trace_id": trace.trace_id,
            "trace_url": trace.trace_url,
            "error": trace.error,
        },
        "variants": {
            name: {"decision": decision.model_dump(mode="json"), "scores": scores[name]}
            for name, decision in variants.items()
        },
        "actual_execution": {
            "calls": len(planner.call_usage),
            "usage": planner.usage,
            "elapsed_ms": int((monotonic() - case_started) * 1000),
        },
    }


def _ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _aggregate(results: list[dict]) -> dict:
    resolvable = sum(item["expected"]["disposition"] == "create_invoice" for item in results)
    review = len(results) - resolvable
    variants: dict[str, dict] = {}
    for variant in ("original", "checked_only", "repaired"):
        score_rows = [item["variants"][variant]["scores"] for item in results]
        variants[variant] = {
            "correct_disposition": _ratio(sum(row["correct_disposition"] for row in score_rows), len(results)),
            "usable_invoice_plan": _ratio(
                sum(row["usable_invoice_plan"] is True for row in score_rows), resolvable
            ),
            "invalid_accepted_draft": _ratio(
                sum(row["invalid_accepted_draft"] for row in score_rows), len(results)
            ),
            "needless_review": _ratio(sum(row["needless_review"] for row in score_rows), resolvable),
        }
    baseline_failures = [
        item
        for item in results
        if item["expected"]["disposition"] == "create_invoice"
        and item["variants"]["original"]["scores"]["usable_invoice_plan"] is False
    ]
    recovered = sum(
        item["variants"]["repaired"]["scores"]["usable_invoice_plan"] is True
        for item in baseline_failures
    )
    total_usage: dict[str, int] = {}
    for item in results:
        for key, value in item["actual_execution"]["usage"].items():
            if isinstance(value, (int, float)):
                total_usage[key] = total_usage.get(key, 0) + int(value)
    ties = sum(
        item["variants"]["original"]["scores"]["correct_disposition"]
        == item["variants"]["repaired"]["scores"]["correct_disposition"]
        for item in results
    )
    regressions = sum(
        item["variants"]["original"]["scores"]["correct_disposition"]
        and not item["variants"]["repaired"]["scores"]["correct_disposition"]
        for item in results
    )
    return {
        "cases": len(results),
        "resolvable_cases": resolvable,
        "review_cases": review,
        "variants": variants,
        "recovery_among_resolvable_baseline_failures": _ratio(recovered, len(baseline_failures)),
        "paired_ties": ties,
        "paired_regressions": regressions,
        "actual_model_calls": sum(item["actual_execution"]["calls"] for item in results),
        "actual_elapsed_ms": sum(item["actual_execution"]["elapsed_ms"] for item in results),
        "actual_token_usage": total_usage,
        "actual_cost": None,
    }


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


async def run_evaluation(
    *, model: str, fault_injection: bool, max_concurrency: int
) -> tuple[Path, dict]:
    selected = evaluation_cases(fault_injection=fault_injection)
    semaphore = asyncio.Semaphore(min(max(1, max_concurrency), 3))

    async def bounded(case: EvaluationCase) -> dict:
        async with semaphore:
            return await _run_case(case, model=model)

    results = await asyncio.gather(*(bounded(case) for case in selected))
    flush_error = flush_traces()
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "run_type": "fault_injection" if fault_injection else "real_model",
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "policy_version": POLICY_VERSION,
        "telemetry_flush_error": flush_error,
        "aggregate": _aggregate(results),
        "cases": results,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(__file__).resolve().parents[1] / "runs" / f"evaluation-{report['run_type']}-{stamp}.json"
    _atomic_write(path, report)
    return path, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the paired invoice-plan evaluation.")
    parser.add_argument("--fault-injection", action="store_true", help="Run the labelled bad-draft demo case.")
    parser.add_argument("--model", default=os.environ.get("FINANCE_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--max-concurrency", type=int, default=3)
    args = parser.parse_args()
    path, report = asyncio.run(
        run_evaluation(
            model=args.model,
            fault_injection=args.fault_injection,
            max_concurrency=args.max_concurrency,
        )
    )
    print(json.dumps({"report": str(path), "aggregate": report["aggregate"]}, indent=2))


if __name__ == "__main__":
    main()
