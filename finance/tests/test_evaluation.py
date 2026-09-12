from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from handoff_finance.engine import improve
from handoff_finance.evaluate import EvaluationCase, score_case
from handoff_finance.fixtures import build_case
from handoff_finance.model_client import OpenAIPlanner
from handoff_finance.models import Decision, InvoiceDraft
from handoff_finance.telemetry import _configured_client_cached, sanitize_trace_data, trace_scope


def _expected_invoice() -> InvoiceDraft:
    return InvoiceDraft(
        action="create_invoice",
        customer_id="customer-demo",
        product_id="implementation",
        currency="SGD",
        quantity=6,
        unit_price_minor=9_000,
        days_until_due=30,
        tax_percent=Decimal("9"),
        po_number="PO-2026-0912",
        contract_id="contract-sep",
        fulfilment_id="fulfilment-sep",
        reason="Any source-backed explanation is acceptable.",
    )


def _decision(*, status: str, selected: InvoiceDraft | None) -> Decision:
    return Decision(
        request_id="showcase",
        objective="Create the invoice.",
        status=status,
        baseline=selected,
        baseline_checks=None,
        selected=selected,
        candidates=[],
        candidate_checks=[],
        elapsed_ms=1,
        usage={},
        error=None,
    )


def test_score_case_accepts_literal_expected_finance_fields_without_matching_reason() -> None:
    expected = _expected_invoice()
    actual = expected.model_copy(update={"reason": "Derived from signed amendment and acceptance."})
    case = EvaluationCase(
        name="showcase",
        request=build_case("showcase"),
        expected_disposition="create_invoice",
        expected_invoice=expected,
        group="real_model",
    )

    score = score_case(case, _decision(status="repaired", selected=actual))

    assert score == {
        "correct_disposition": True,
        "usable_invoice_plan": True,
        "invalid_accepted_draft": False,
        "needless_review": False,
    }


def test_score_case_counts_review_on_resolvable_case_as_failure() -> None:
    case = EvaluationCase(
        name="showcase",
        request=build_case("showcase"),
        expected_disposition="create_invoice",
        expected_invoice=_expected_invoice(),
        group="real_model",
    )

    score = score_case(case, _decision(status="needs_review", selected=None))

    assert score["correct_disposition"] is False
    assert score["usable_invoice_plan"] is False
    assert score["invalid_accepted_draft"] is False
    assert score["needless_review"] is True


def test_score_case_distinguishes_correct_review_from_invalid_acceptance() -> None:
    case = EvaluationCase(
        name="conflicting_latest_contracts",
        request=build_case("conflicting_latest_contracts"),
        expected_disposition="request_review",
        expected_invoice=None,
        group="real_model",
    )

    correct_review = score_case(case, _decision(status="needs_review", selected=None))
    invalid_acceptance = score_case(
        case,
        _decision(status="accepted", selected=_expected_invoice()),
    )

    assert correct_review == {
        "correct_disposition": True,
        "usable_invoice_plan": None,
        "invalid_accepted_draft": False,
        "needless_review": False,
    }
    assert invalid_acceptance["correct_disposition"] is False
    assert invalid_acceptance["invalid_accepted_draft"] is True


def test_trace_scope_is_explicitly_local_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_TRACE_DURING_TESTS", "1")
    monkeypatch.setattr("handoff_finance.telemetry._root_env", lambda: {})
    _configured_client_cached.cache_clear()

    with trace_scope("evaluate-invoice", {"request_id": "case-1"}) as trace:
        trace.set_output({"status": "accepted"})

    assert trace.status == "local"
    assert trace.trace_id is None
    assert trace.trace_url is None
    assert trace.error is None
    _configured_client_cached.cache_clear()


def test_trace_data_masks_secret_keys_and_signed_url_queries() -> None:
    safe = sanitize_trace_data(
        {
            "request_id": "case-1",
            "api_key": "do-not-log",
            "nested": {"authorization": "Bearer secret"},
            "document": "https://example.test/file.pdf?X-Amz-Signature=secret&download=1",
        }
    )

    assert safe == {
        "request_id": "case-1",
        "api_key": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]"},
        "document": "https://example.test/file.pdf?[REDACTED]",
    }


def test_trace_scope_does_not_probe_trace_id_before_span_is_active(monkeypatch) -> None:
    class FakeSpan:
        def update(self, **kwargs):
            pass

    class FakeContext:
        def __init__(self, client):
            self.client = client

        def __enter__(self):
            self.client.active = True
            return FakeSpan()

        def __exit__(self, *args):
            self.client.active = False

    class FakeClient:
        active = False

        def start_as_current_observation(self, **kwargs):
            return FakeContext(self)

        def get_current_trace_id(self):
            assert self.active, "trace ID was queried before an observation became active"
            return "a" * 32

        def get_trace_url(self, **kwargs):
            return "https://example.test/trace"

    monkeypatch.setattr("handoff_finance.telemetry._configured_client", lambda: (FakeClient(), None))

    with trace_scope("evaluate-invoice", {"request_id": "case-1"}) as trace:
        assert trace.trace_id == "a" * 32


def test_source_blocker_returns_review_without_spending_a_repair_call() -> None:
    blocked = build_case("unaccepted_fulfilment")
    baseline = blocked.proposed
    request = blocked.model_copy(update={"proposed": None})

    class Planner:
        usage = {}
        repair_calls = 0

        async def initial(self, request):
            return baseline

        async def repair(self, request, baseline, checks):
            self.repair_calls += 1
            raise AssertionError("source evidence cannot be repaired by changing the draft")

    planner = Planner()

    decision = asyncio.run(improve(request, planner))

    assert decision.status == "needs_review"
    assert decision.baseline_checks is not None
    assert decision.baseline_checks.review_reasons
    assert planner.repair_calls == 0


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    total_tokens: int

    def model_dump(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


class _Response:
    def __init__(self, parsed, usage: _Usage):
        self.output_parsed = parsed
        self.usage = usage


class _Responses:
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    async def parse(self, **kwargs):
        parsed, usage = next(self.outputs)
        if isinstance(parsed, InvoiceDraft):
            parsed = kwargs["text_format"].model_validate(parsed.model_dump(mode="json"))
        elif isinstance(parsed, dict):
            parsed = kwargs["text_format"].model_validate(parsed)
        return _Response(parsed, usage)


class _Client:
    def __init__(self, outputs):
        self.responses = _Responses(outputs)


def test_planner_accumulates_usage_across_initial_and_repair_calls() -> None:
    request = build_case("showcase").model_copy(update={"proposed": None})
    baseline = build_case("showcase").proposed
    repaired = _expected_invoice()
    planner = OpenAIPlanner(
        client=_Client(
            [
                (baseline, _Usage(100, 20, 120)),
                ({"candidates": [repaired.model_dump(mode="json")]}, _Usage(200, 30, 230)),
            ]
        ),
        model="gpt-4.1-mini",
        api_key="unused",
    )

    async def run() -> None:
        initial = await planner.initial(request)
        from handoff_finance.rules import validate_invoice

        gate = validate_invoice(request, initial)
        await planner.repair(request, initial, gate)

    asyncio.run(run())

    assert planner.usage == {"input_tokens": 300, "output_tokens": 50, "total_tokens": 350}
    assert planner.call_usage == [
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        {"input_tokens": 200, "output_tokens": 30, "total_tokens": 230},
    ]
