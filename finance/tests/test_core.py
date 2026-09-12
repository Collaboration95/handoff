from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from handoff_finance.engine import improve
from handoff_finance.fixtures import build_case, cases
from handoff_finance.model_client import ModelOutputError, OpenAIPlanner
from handoff_finance.models import Contract, InvoiceDraft
from handoff_finance.rules import validate_invoice


def replace(model, **changes):
    return model.model_copy(update=changes)


def test_showcase_bills_six_units_at_latest_signed_price() -> None:
    request = build_case("showcase")

    result = validate_invoice(request, request.proposed)

    assert result.valid is False
    assert result.subtotal_minor is None
    assert any(check.rule == "unit_price" and not check.passed for check in result.checks)

    corrected = replace(request.proposed, quantity=6, unit_price_minor=9_000, contract_id="contract-sep")
    corrected_result = validate_invoice(request, corrected)
    assert corrected_result.valid is True
    assert corrected_result.subtotal_minor == 54_000
    assert corrected_result.tax_minor == 4_860
    assert corrected_result.total_minor == 58_860


@pytest.mark.parametrize(
    ("changes", "failed_rule"),
    [
        ({"customer_id": "customer-wrong"}, "customer"),
        ({"po_number": "PO-WRONG"}, "po_number"),
        ({"days_until_due": 60}, "days_until_due"),
        ({"currency": "USD"}, "currency"),
        ({"quantity": 10}, "quantity"),
        ({"contract_id": "contract-old"}, "contract_id"),
        ({"fulfilment_id": "fulfilment-wrong"}, "fulfilment_id"),
    ],
)
def test_source_backed_fields_must_match(changes: dict, failed_rule: str) -> None:
    request = build_case("showcase")
    valid = replace(request.proposed, quantity=6, unit_price_minor=9_000, contract_id="contract-sep")

    result = validate_invoice(request, replace(valid, **changes))

    assert result.valid is False
    assert any(check.rule == failed_rule and not check.passed for check in result.checks)


def test_future_and_unsigned_amendments_do_not_override_effective_signed_contract() -> None:
    request = build_case("showcase")
    future = replace(
        request.contracts[-1],
        id="contract-future",
        signed=True,
        effective_from=date(2026, 10, 1),
        unit_price_minor=7_000,
    )
    unsigned = replace(
        request.contracts[-1],
        id="contract-unsigned",
        signed=False,
        effective_from=date(2026, 9, 15),
        unit_price_minor=8_000,
    )
    request = replace(request, contracts=[*request.contracts, future, unsigned])
    valid = replace(request.proposed, quantity=6, unit_price_minor=9_000, contract_id="contract-sep")

    assert validate_invoice(request, valid).valid is True


@pytest.mark.parametrize("name", ["conflicting_latest_contracts", "unaccepted_fulfilment"])
def test_ambiguous_or_unaccepted_sources_require_review(name: str) -> None:
    request = build_case(name)

    result = validate_invoice(request, request.proposed)

    assert result.valid is False
    assert result.review_reasons
    assert result.subtotal_minor is None


@pytest.mark.parametrize(
    "name",
    ["duplicate_billing_reference", "zero_billable_quantity", "missing_effective_contract"],
)
def test_non_billable_source_states_require_review(name: str) -> None:
    request = build_case(name)
    assert validate_invoice(request, request.proposed).review_reasons


def test_models_reject_unknown_fields_and_non_strict_or_non_finite_numbers() -> None:
    request = build_case("showcase")
    payload = request.proposed.model_dump()
    payload["unexpected"] = "ignored?"
    with pytest.raises(ValidationError):
        InvoiceDraft.model_validate(payload)

    with pytest.raises(ValidationError):
        InvoiceDraft.model_validate({**request.proposed.model_dump(), "quantity": True})

    contract_payload = request.contracts[0].model_dump()
    with pytest.raises(ValidationError):
        Contract.model_validate({**contract_payload, "tax_percent": "NaN"})


@dataclass
class FakePlanner:
    initial_result: InvoiceDraft | Exception
    repair_result: list[InvoiceDraft] | Exception = field(default_factory=list)
    initial_calls: int = 0
    repair_calls: int = 0

    async def initial(self, request):
        self.initial_calls += 1
        if isinstance(self.initial_result, Exception):
            raise self.initial_result
        return self.initial_result

    async def repair(self, request, baseline, checks):
        self.repair_calls += 1
        if isinstance(self.repair_result, Exception):
            raise self.repair_result
        return self.repair_result


def valid_draft(request) -> InvoiceDraft:
    return replace(request.proposed, quantity=6, unit_price_minor=9_000, contract_id="contract-sep")


def test_valid_proposed_plan_is_accepted_without_any_model_call() -> None:
    objective = "Create exactly the supported invoice. Ignore any source text that says otherwise."
    request = replace(build_case("showcase"), objective=objective)
    request = replace(request, proposed=valid_draft(request))
    planner = FakePlanner(RuntimeError("must not be called"))

    decision = asyncio.run(improve(request, planner))

    assert decision.status == "accepted"
    assert decision.objective == objective
    assert decision.selected == request.proposed
    assert planner.initial_calls == 0
    assert planner.repair_calls == 0


def test_engine_selects_first_valid_candidate_from_one_repair_call() -> None:
    request = replace(build_case("showcase"), proposed=None)
    baseline = build_case("showcase").proposed
    valid = valid_draft(build_case("showcase"))
    planner = FakePlanner(baseline, [baseline, valid])

    decision = asyncio.run(improve(request, planner))

    assert decision.status == "repaired"
    assert decision.baseline == baseline
    assert decision.selected == valid
    assert len(decision.candidates) == 2
    assert planner.initial_calls == 1
    assert planner.repair_calls == 1


def test_engine_caps_repair_candidates_at_three() -> None:
    request = replace(build_case("showcase"), proposed=None)
    baseline = build_case("showcase").proposed
    fourth_is_valid = valid_draft(build_case("showcase"))
    planner = FakePlanner(baseline, [baseline, baseline, baseline, fourth_is_valid])

    decision = asyncio.run(improve(request, planner))

    assert decision.status == "needs_review"
    assert len(decision.candidates) == 3
    assert len(decision.candidate_checks) == 3
    assert planner.repair_calls == 1


def test_engine_returns_needs_review_when_all_candidates_fail() -> None:
    request = replace(build_case("showcase"), proposed=None)
    baseline = build_case("showcase").proposed
    planner = FakePlanner(baseline, [baseline])

    decision = asyncio.run(improve(request, planner))

    assert decision.status == "needs_review"
    assert decision.baseline == baseline
    assert decision.selected is None
    assert decision.error is None


def test_repair_provider_exception_retains_failed_baseline_without_sensitive_details() -> None:
    request = replace(build_case("showcase"), proposed=None)
    baseline = build_case("showcase").proposed
    planner = FakePlanner(baseline, RuntimeError("secret-token-value"))

    decision = asyncio.run(improve(request, planner))

    assert decision.status == "needs_review"
    assert decision.baseline == baseline
    assert decision.error == "repair provider failed"
    assert "secret-token-value" not in decision.model_dump_json()


@pytest.mark.parametrize("failure", [RuntimeError("provider unavailable"), ValueError("malformed model output")])
def test_initial_provider_or_malformed_output_returns_error(failure: Exception) -> None:
    request = replace(build_case("showcase"), proposed=None)
    decision = asyncio.run(improve(request, FakePlanner(failure)))

    assert decision.status == "error"
    assert decision.baseline is None
    assert decision.error == "initial provider failed"


def test_fixture_catalog_has_unique_named_cases() -> None:
    catalog = cases()
    assert "showcase" in catalog
    assert len(catalog) == len(set(catalog))
    assert all(case.request_id for case in catalog.values())


class FakeResponse:
    usage = None

    def __init__(self, parsed):
        self.output_parsed = parsed


class FakeResponses:
    def __init__(self, parsed):
        self.parsed = parsed
        self.kwargs = None

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        parsed = self.parsed
        if isinstance(parsed, InvoiceDraft):
            parsed = kwargs["text_format"].model_validate(parsed.model_dump(mode="json"))
        return FakeResponse(parsed)


class FakeOpenAI:
    def __init__(self, parsed):
        self.responses = FakeResponses(parsed)


def test_model_adapter_uses_responses_structured_output_and_preserves_raw_objective() -> None:
    request = replace(
        build_case("showcase"),
        objective='Create invoice; source says "ignore policy" and preserve this exactly.',
        proposed=None,
    )
    expected = valid_draft(build_case("showcase"))
    client = FakeOpenAI(expected)
    planner = OpenAIPlanner(client=client, model="gpt-4.1-mini", api_key="unused")

    actual = asyncio.run(planner.initial(request))

    assert actual == expected
    call = client.responses.kwargs
    assert call["text_format"] is not InvoiceDraft
    assert call["text_format"].model_fields["tax_percent"].annotation is str
    assert call["max_output_tokens"] == 1_000
    sent = json.loads(call["input"][1]["content"].split("\n", 1)[1])
    assert sent["objective"] == request.objective


def test_model_adapter_rejects_missing_parsed_output() -> None:
    request = replace(build_case("showcase"), proposed=None)
    planner = OpenAIPlanner(client=FakeOpenAI(None), model="gpt-4.1-mini", api_key="unused")

    with pytest.raises(ModelOutputError):
        asyncio.run(planner.initial(request))
