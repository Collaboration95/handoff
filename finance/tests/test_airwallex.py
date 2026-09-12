from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

import handoff_finance.airwallex as airwallex_module
from handoff_finance.airwallex import AirwallexClient, AirwallexError, CommandResult
from handoff_finance.fixtures import build_case


CLI = "/demo/.tools/airwallex"
COMMON = ("--compact", "--no-telemetry")


def valid_case():
    request = build_case("showcase")
    draft = request.proposed.model_copy(
        update={"quantity": 6, "unit_price_minor": 9_000, "contract_id": "contract-sep"}
    )
    return request, draft


def ok(data: dict) -> CommandResult:
    return CommandResult(0, json.dumps({"ok": True, "data": data}), "")


class ScriptedRunner:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []

    async def __call__(self, argv, stdin, timeout):
        self.calls.append((tuple(argv), stdin, timeout))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def authenticated() -> CommandResult:
    return ok(
        {
            "is_authenticated": True,
            "mode": "sandbox",
            "base_url": "https://api.sandbox.airwallex.com",
        }
    )


def invoice_record() -> dict:
    return {
        "id": "inv_demo_123",
        "status": "DRAFT",
        "collection_method": "OUT_OF_BAND",
        "billing_customer_id": "customer-demo",
        "currency": "SGD",
        "days_until_due": 30,
        "metadata": {
            "handoff_reference": "BILL-2026-09",
            "contract_id": "contract-sep",
            "fulfilment_id": "fulfilment-sep",
            "po_number": "PO-2026-0912",
        },
        "total_amount": 588.6,
        "hosted_invoice_url": "https://sandbox.airwallex.example/inv_demo_123",
    }


def line_record() -> dict:
    return {
        "id": "line_demo_123",
        "description": "Implementation services — BILL-2026-09",
        "price": {
            "pricing_model": "PER_UNIT",
            "product_id": "implementation",
            "unit_amount": 90,
            "tax_included": False,
        },
        "quantity": 6,
        "tax_percent": 9,
        "metadata": {"contract_id": "contract-sep", "fulfilment_id": "fulfilment-sep"},
    }


def test_create_uses_fixed_cli_commands_exact_financial_payload_and_readback(tmp_path) -> None:
    """Catches unsafe argv, minor-unit leakage, schema drift, and skipped readback checks."""
    request, draft = valid_case()
    runner = ScriptedRunner(
        authenticated(),
        ok({"id": "inv_demo_123", "status": "DRAFT"}),
        ok({"items": [line_record()]}),
        ok(invoice_record()),
        ok({"items": [line_record()]}),
    )
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.create_verified_invoice(request, draft))

    assert result == {
        "status": "success",
        "invoice_id": "inv_demo_123",
        "state": "DRAFT",
        "stage": "verified",
        "verified": True,
        "checks": {
            "state": True,
            "collection_method": True,
            "billing_customer_id": True,
            "currency": True,
            "days_until_due": True,
            "po_number": True,
            "total_amount": True,
            "line_count": True,
            "line_product_id": True,
            "line_unit_amount": True,
            "line_quantity": True,
            "line_tax_percent": True,
            "line_source_ids": True,
        },
        "error": None,
        "environment": "sandbox",
        "hosted_url": "https://sandbox.airwallex.example/inv_demo_123",
    }
    assert [call[0] for call in runner.calls] == [
        (CLI, "auth", "whoami", *COMMON),
        (CLI, "invoices", "create", "--data-stdin", *COMMON, "--confirm"),
        (CLI, "invoices", "line-items", "add", "inv_demo_123", "--data-stdin", *COMMON, "--confirm"),
        (CLI, "invoices", "get", "inv_demo_123", *COMMON),
        (CLI, "invoices", "line-items", "list", "inv_demo_123", *COMMON),
    ]
    create_payload = json.loads(runner.calls[1][1])
    assert create_payload == {
        "billing_customer_id": "customer-demo",
        "currency": "SGD",
        "request_id": create_payload["request_id"],
        "collection_method": "OUT_OF_BAND",
        "days_until_due": 30,
        "default_tax_percent": 9,
        "memo": "Draft created by Handoff finance demo.",
        "metadata": {
            "handoff_reference": "BILL-2026-09",
            "contract_id": "contract-sep",
            "fulfilment_id": "fulfilment-sep",
            "po_number": "PO-2026-0912",
        },
    }
    line_payload = json.loads(runner.calls[2][1])
    assert line_payload == {
        "request_id": line_payload["request_id"],
        "line_items": [
            {
                "description": "Implementation services — BILL-2026-09",
                "price": {
                    "pricing_model": "PER_UNIT",
                    "product_id": "implementation",
                    "unit_amount": 90,
                    "tax_included": False,
                },
                "quantity": 6,
                "tax_percent": 9,
                "metadata": {"contract_id": "contract-sep", "fulfilment_id": "fulfilment-sep"},
            }
        ],
    }
    assert isinstance(line_payload["line_items"][0]["price"]["unit_amount"], int)
    assert all(call[1] is None for call in (runner.calls[0], runner.calls[3], runner.calls[4]))


def test_invalid_core_draft_is_rejected_before_auth_or_financial_writes(tmp_path) -> None:
    """Catches any path that lets an unvalidated model draft reach Airwallex."""
    request, draft = valid_case()
    draft = draft.model_copy(update={"unit_price_minor": 8_999})
    runner = ScriptedRunner()
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.create_verified_invoice(request, draft))

    assert result["status"] == "rejected"
    assert result["stage"] == "validation"
    assert result["verified"] is False
    assert runner.calls == []


def test_verify_existing_invoice_reads_current_record_without_writes_or_journal_changes(tmp_path) -> None:
    """Catches duplicate creation and trusting a previous success without fresh readback."""
    request, draft = valid_case()
    runner = ScriptedRunner(authenticated(), ok(invoice_record()), ok({"items": [line_record()]}))
    journal = tmp_path / "journal.json"
    journal.write_text('{"invoices":{"previous":{"stage":"verified"}}}\n')
    original_journal = journal.read_bytes()
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=journal)

    result = asyncio.run(client.verify_existing_invoice(request, draft, "inv_demo_123"))

    assert result["status"] == "existing"
    assert result["stage"] == "verified"
    assert result["verified"] is True
    assert result["reused_existing"] is True
    assert result["invoice_id"] == "inv_demo_123"
    assert result["state"] == "DRAFT"
    assert result["hosted_url"] == "https://sandbox.airwallex.example/inv_demo_123"
    assert len(result["checks"]) == 16
    assert all(result["checks"].values())
    assert [call[0] for call in runner.calls] == [
        (CLI, "auth", "whoami", *COMMON),
        (CLI, "invoices", "get", "inv_demo_123", *COMMON),
        (CLI, "invoices", "line-items", "list", "inv_demo_123", *COMMON),
    ]
    assert all(call[1] is None for call in runner.calls)
    assert journal.read_bytes() == original_journal


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        ("id", "inv_unrelated", "invoice_id"),
        ("total_amount", 589, "total_amount"),
        ("status", "FINALIZED", "state"),
        ("handoff_reference", "OTHER-BILL", "billing_reference"),
        ("contract_id", "contract-old", "invoice_source_ids"),
        ("fulfilment_id", "fulfilment-other", "invoice_source_ids"),
    ],
)
def test_verify_existing_invoice_blocks_mismatched_record_without_recreating(
    tmp_path, field, value, failed_check
) -> None:
    """Catches reusing an unrelated, changed, or finalized invoice as a verified draft."""
    request, draft = valid_case()
    invoice = invoice_record()
    if field in {"handoff_reference", "contract_id", "fulfilment_id"}:
        invoice["metadata"][field] = value
    else:
        invoice[field] = value
    runner = ScriptedRunner(authenticated(), ok(invoice), ok({"items": [line_record()]}))
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.verify_existing_invoice(request, draft, "inv_demo_123"))

    assert result["verified"] is False
    assert result["stage"] == "verification_failed"
    assert result["error"] == "readback_mismatch"
    assert result["checks"][failed_check] is False
    assert not result.get("reused_existing")
    assert "hosted_url" not in result
    assert all("--confirm" not in call[0] and call[1] is None for call in runner.calls)
    assert not client.journal_path.exists()


@pytest.mark.parametrize("unsafe_id", ["../inv_demo", "--help", ""])
def test_verify_existing_invoice_rejects_unsafe_ids_before_cli(tmp_path, unsafe_id) -> None:
    """Catches untrusted invoice IDs reaching the CLI command boundary."""
    request, draft = valid_case()
    runner = ScriptedRunner()
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.verify_existing_invoice(request, draft, unsafe_id))

    assert result["verified"] is False
    assert result["error"] == "unsafe_resource_id"
    assert runner.calls == []


def test_verify_existing_invoice_rechecks_core_before_cli(tmp_path) -> None:
    """Catches a changed, invalid approved selection passing through reconciliation."""
    request, draft = valid_case()
    runner = ScriptedRunner()
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(
        client.verify_existing_invoice(request, draft.model_copy(update={"quantity": 10}), "inv_demo_123")
    )

    assert result["verified"] is False
    assert result["error"] == "core_validation_failed"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (asyncio.TimeoutError(), "timeout"),
        (CommandResult(1, "", "forbidden: secret"), "permission_error"),
        (CommandResult(0, "not-json", ""), "invalid_cli_response"),
    ],
)
def test_verify_existing_invoice_failed_read_is_nonverified_and_never_creates(tmp_path, response, error) -> None:
    """Catches treating failed readback as a verified reuse or a reason to create again."""
    request, draft = valid_case()
    runner = ScriptedRunner(authenticated(), response)
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.verify_existing_invoice(request, draft, "inv_demo_123"))

    assert result["verified"] is False
    assert result["invoice_id"] == "inv_demo_123"
    assert result["error"] == error
    assert not result.get("reused_existing")
    assert "secret" not in json.dumps(result)
    assert all("--confirm" not in call[0] and call[1] is None for call in runner.calls)


def test_verify_existing_invoice_refuses_production_before_invoice_reads(tmp_path) -> None:
    """Catches using sandbox invoice IDs in a globally selected production CLI profile."""
    request, draft = valid_case()
    runner = ScriptedRunner(ok({"is_authenticated": True, "mode": "production", "base_url": "https://api.airwallex.com"}))
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.verify_existing_invoice(request, draft, "inv_demo_123"))

    assert result["verified"] is False
    assert result["error"] == "sandbox_required"
    assert len(runner.calls) == 1


def test_add_line_failure_retains_partial_invoice_and_never_recreates(tmp_path) -> None:
    """Catches loss of the real invoice ID and duplicate creation after a partial write."""
    request, draft = valid_case()
    runner = ScriptedRunner(
        authenticated(),
        ok({"id": "inv_partial_123", "status": "DRAFT"}),
        CommandResult(1, "", '{"error":{"code":"billing_disabled","message":"Billing is unavailable"}}'),
    )
    journal = tmp_path / "journal.json"
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=journal)

    first = asyncio.run(client.create_verified_invoice(request, draft))
    second = asyncio.run(client.create_verified_invoice(request, draft))

    assert first["status"] == "partial"
    assert first["invoice_id"] == "inv_partial_123"
    assert first["state"] == "DRAFT"
    assert first["stage"] == "line_items_failed"
    assert first["verified"] is False
    assert first["error"] == "billing_error"
    assert second == first
    assert len(runner.calls) == 3
    saved = json.loads(journal.read_text())["invoices"]["showcase"]
    assert saved["invoice_id"] == "inv_partial_123"
    assert saved["stage"] == "line_items_failed"


def test_create_timeout_is_uncertain_and_repeat_does_not_issue_another_write(tmp_path) -> None:
    """Catches blind retry after an uncertain create that could duplicate an invoice."""
    request, draft = valid_case()
    runner = ScriptedRunner(authenticated(), asyncio.TimeoutError())
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    first = asyncio.run(client.create_verified_invoice(request, draft))
    second = asyncio.run(client.create_verified_invoice(request, draft))

    assert first["status"] == "unknown"
    assert first["invoice_id"] is None
    assert first["stage"] == "create_uncertain"
    assert first["error"] == "timeout"
    assert second == first
    assert len(runner.calls) == 2


def test_malformed_create_success_is_uncertain_and_repeat_does_not_write(tmp_path) -> None:
    """Catches treating an unreadable post-create response as a safe-to-retry rejection."""
    request, draft = valid_case()
    runner = ScriptedRunner(authenticated(), CommandResult(0, "not-json", ""))
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    first = asyncio.run(client.create_verified_invoice(request, draft))
    second = asyncio.run(client.create_verified_invoice(request, draft))

    assert first["status"] == "unknown"
    assert first["stage"] == "create_uncertain"
    assert first["error"] == "invalid_cli_response"
    assert second == first
    assert len(runner.calls) == 2


def test_interrupted_create_was_journaled_before_write_and_is_not_repeated(tmp_path) -> None:
    """Catches the process-crash window between issuing create and recording uncertainty."""
    request, draft = valid_case()
    runner = ScriptedRunner(authenticated(), asyncio.CancelledError())
    journal = tmp_path / "journal.json"
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=journal)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.create_verified_invoice(request, draft))
    repeated = asyncio.run(client.create_verified_invoice(request, draft))

    assert repeated["status"] == "unknown"
    assert repeated["stage"] == "create_uncertain"
    assert repeated["error"] == "operation_in_progress"
    assert len(runner.calls) == 2


def test_malformed_add_line_success_is_uncertain_and_repeat_does_not_write(tmp_path) -> None:
    """Catches treating unreadable post-add output as a definite line-item failure."""
    request, draft = valid_case()
    runner = ScriptedRunner(
        authenticated(),
        ok({"id": "inv_uncertain_123", "status": "DRAFT"}),
        CommandResult(0, "not-json", ""),
    )
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    first = asyncio.run(client.create_verified_invoice(request, draft))
    second = asyncio.run(client.create_verified_invoice(request, draft))

    assert first["status"] == "partial"
    assert first["invoice_id"] == "inv_uncertain_123"
    assert first["stage"] == "line_items_uncertain"
    assert first["error"] == "invalid_cli_response"
    assert second == first
    assert len(runner.calls) == 3


def test_same_request_id_with_changed_financial_content_is_a_conflict(tmp_path) -> None:
    """Catches reuse of an idempotency journal entry for changed financial intent."""
    request, draft = valid_case()
    runner = ScriptedRunner(authenticated(), asyncio.TimeoutError())
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")
    asyncio.run(client.create_verified_invoice(request, draft))

    changed = draft.model_copy(update={"reason": "same finance fields, changed approved content"})
    result = asyncio.run(client.create_verified_invoice(request, changed))

    assert result["status"] == "conflict"
    assert result["stage"] == "journal"
    assert result["error"] == "request_content_changed"
    assert len(runner.calls) == 2


def test_readiness_rejects_authenticated_production_context_without_secrets(tmp_path) -> None:
    """Catches writes accidentally inheriting a globally selected production profile."""
    runner = ScriptedRunner(
        ok(
            {
                "is_authenticated": True,
                "mode": "production",
                "base_url": "https://api.airwallex.com",
                "access_token": "must-not-leak",
            }
        )
    )
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    result = asyncio.run(client.readiness())

    assert result == {
        "ready": False,
        "authenticated": True,
        "environment": "production",
        "error": "sandbox_required",
    }
    assert "must-not-leak" not in json.dumps(result)


def test_ensure_demo_objects_creates_then_reads_back_and_reuses_receipt(tmp_path) -> None:
    """Catches real-data listing, omitted readback, and duplicate demo object creation."""
    customer = {
        "id": "cus_demo_123",
        "name": "Handoff Demo Customer",
        "default_billing_currency": "SGD",
        "address": {"country_code": "SG"},
        "metadata": {"handoff_demo": "true"},
    }
    product = {
        "id": "prod_demo_123",
        "name": "Handoff Demo Implementation Unit",
        "description": "Synthetic implementation unit for the Handoff demo.",
        "metadata": {"handoff_demo": "true"},
    }
    runner = ScriptedRunner(
        authenticated(), ok(customer), ok(customer), ok(product), ok(product),
        authenticated(), ok(customer), ok(product),
    )
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    first = asyncio.run(client.ensure_demo_objects())
    second = asyncio.run(client.ensure_demo_objects())

    assert first == second == {"customer_id": "cus_demo_123", "product_id": "prod_demo_123"}
    assert [call[0] for call in runner.calls] == [
        (CLI, "auth", "whoami", *COMMON),
        (CLI, "billing-customers", "create", "--data-stdin", *COMMON, "--confirm"),
        (CLI, "billing-customers", "get", "cus_demo_123", *COMMON),
        (CLI, "products", "create", "--data-stdin", *COMMON, "--confirm"),
        (CLI, "products", "get", "prod_demo_123", *COMMON),
        (CLI, "auth", "whoami", *COMMON),
        (CLI, "billing-customers", "get", "cus_demo_123", *COMMON),
        (CLI, "products", "get", "prod_demo_123", *COMMON),
    ]
    customer_payload = json.loads(runner.calls[1][1])
    product_payload = json.loads(runner.calls[3][1])
    assert customer_payload == {
        "request_id": customer_payload["request_id"],
        "name": "Handoff Demo Customer",
        "default_billing_currency": "SGD",
        "address": {"country_code": "SG"},
        "metadata": {"handoff_demo": "true"},
    }
    assert "email" not in customer_payload
    assert product_payload == {
        "request_id": product_payload["request_id"],
        "name": "Handoff Demo Implementation Unit",
        "description": "Synthetic implementation unit for the Handoff demo.",
        "metadata": {"handoff_demo": "true"},
    }


def test_uncertain_demo_customer_create_is_journaled_and_never_repeated(tmp_path) -> None:
    """Catches duplicate customer creation after a timeout with no returned customer ID."""
    runner = ScriptedRunner(authenticated(), asyncio.TimeoutError())
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    for _ in range(2):
        with pytest.raises(AirwallexError, match="demo_customer_create_uncertain"):
            asyncio.run(client.ensure_demo_objects())

    writes = [call for call in runner.calls if call[0][1:3] == ("billing-customers", "create")]
    assert len(writes) == 1


def test_uncertain_demo_product_create_is_journaled_and_never_repeated(tmp_path) -> None:
    """Catches duplicate product creation after an unreadable successful response."""
    customer = {
        "id": "cus_demo_123",
        "name": "Handoff Demo Customer",
        "default_billing_currency": "SGD",
        "address": {"country_code": "SG"},
        "metadata": {"handoff_demo": "true"},
    }
    runner = ScriptedRunner(
        authenticated(), ok(customer), ok(customer), CommandResult(0, "not-json", "")
    )
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    for _ in range(2):
        with pytest.raises(AirwallexError, match="demo_product_create_uncertain"):
            asyncio.run(client.ensure_demo_objects())

    writes = [call for call in runner.calls if call[0][1:3] == ("products", "create")]
    assert len(writes) == 1


def test_cli_errors_are_classified_without_echoing_sensitive_stderr(tmp_path) -> None:
    """Catches flattened operational errors and secret-bearing CLI diagnostics."""
    request, draft = valid_case()
    for message, expected in [
        ("forbidden: missing permission Authorization: Bearer secret", "permission_error"),
        ("request rejected because source IP is not allowlisted secret", "ip_restriction"),
        ("billing capability is not enabled secret", "billing_error"),
    ]:
        runner = ScriptedRunner(authenticated(), CommandResult(1, "", message))
        client = AirwallexClient(
            runner=runner,
            cli_path=CLI,
            journal_path=tmp_path / f"{expected}.json",
        )
        result = asyncio.run(client.create_verified_invoice(request, draft))
        assert result["error"] == expected
        assert "secret" not in json.dumps(result)


def test_invoice_cli_calls_trace_actual_runner_and_decoded_safe_data(monkeypatch, tmp_path) -> None:
    """Catches post-hoc spans, auth tracing, and secrets or signed queries in trace data."""
    events = []
    active_span = None

    class Scope:
        def __init__(self, name, input, **kwargs):
            self.name = name
            self.input = input
            self.kwargs = kwargs

        def __enter__(self):
            nonlocal active_span
            assert active_span is None
            active_span = self.name
            events.append(("start", self.name, self.input, self.kwargs))
            return self

        def set_output(self, output):
            events.append(("output", self.name, output))

        def __exit__(self, *args):
            nonlocal active_span
            events.append(("end", self.name))
            active_span = None

    def fake_trace_scope(name, input, **kwargs):
        return Scope(name, input, **kwargs)

    responses = deque(
        [
            authenticated(),
            ok({"id": "inv_demo_123", "access_token": "response-secret"}),
            ok({"items": [{"id": "line_demo_123"}]}),
            ok(
                {
                    "id": "inv_demo_123",
                    "hosted_invoice_url": (
                        "https://example.test/invoice?X-Amz-Signature=response-secret"
                    ),
                }
            ),
            ok({"items": [{"id": "line_demo_123"}]}),
        ]
    )

    async def runner(argv, stdin, timeout):
        if argv[1:3] == ("auth", "whoami"):
            assert active_span is None
        else:
            assert active_span is not None
        return responses.popleft()

    monkeypatch.setattr(airwallex_module, "trace_scope", fake_trace_scope)
    client = AirwallexClient(runner=runner, cli_path=CLI, journal_path=tmp_path / "journal.json")

    async def exercise_calls():
        await client._call("auth", "whoami")
        await client._call(
            "invoices", "create", payload={"request_id": "req-1", "api_key": "input-secret"}, write=True
        )
        await client._call(
            "invoices",
            "line-items",
            "add",
            "inv_demo_123",
            payload={"request_id": "req-1", "line_items": []},
            write=True,
        )
        await client._call("invoices", "get", "inv_demo_123")
        await client._call("invoices", "line-items", "list", "inv_demo_123")

    asyncio.run(exercise_calls())

    starts = [event for event in events if event[0] == "start"]
    outputs = [event for event in events if event[0] == "output"]
    assert [event[1] for event in starts] == [
        "airwallex-invoice-create",
        "airwallex-line-items-add",
        "airwallex-invoice-get",
        "airwallex-line-items-list",
    ]
    assert all(event[3]["as_type"] == "tool" for event in starts)
    assert starts[0][2]["payload"]["api_key"] == "[REDACTED]"
    assert outputs[0][2]["access_token"] == "[REDACTED]"
    assert outputs[2][2]["hosted_invoice_url"] == "https://example.test/invoice?[REDACTED]"
    assert len([event for event in events if event[0] == "end"]) == 4
