from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .models import InvoiceDraft, InvoiceRequest
from .rules import validate_invoice
from .telemetry import sanitize_trace_data, trace_scope


SANDBOX_URL = "https://api.sandbox.airwallex.com"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_WRITE_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], str | None, float], Awaitable[CommandResult]]


class AirwallexError(RuntimeError):
    """Credential-free adapter failure safe to show at the application boundary."""

    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


async def _subprocess_runner(argv: Sequence[str], stdin: str | None, timeout: float) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return CommandResult(process.returncode, stdout.decode(), stderr.decode())


def _numeric(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise AirwallexError("unsafe_resource_id")
    return value


def _classify_error(text: str) -> str:
    lowered = text.lower()
    if "allowlist" in lowered or "whitelist" in lowered or "source ip" in lowered:
        return "ip_restriction"
    if "permission" in lowered or "forbidden" in lowered or "unauthorized" in lowered:
        return "permission_error"
    if "billing" in lowered or "capability" in lowered:
        return "billing_error"
    if "not authenticated" in lowered or "sign in" in lowered or "login" in lowered:
        return "not_authenticated"
    return "cli_error"


def _result(
    status: str,
    stage: str,
    *,
    invoice_id: str | None = None,
    state: str | None = None,
    verified: bool = False,
    checks: Mapping[str, bool] | None = None,
    error: str | None = None,
    hosted_url: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "invoice_id": invoice_id,
        "state": state,
        "stage": stage,
        "verified": verified,
        "checks": dict(checks or {}),
        "error": error,
        "environment": "sandbox",
    }
    if hosted_url:
        result["hosted_url"] = hosted_url
    return result


class AirwallexClient:
    """Sandbox-only adapter for the official Airwallex CLI."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        cli_path: str | Path | None = None,
        journal_path: str | Path | None = None,
        timeout: float = 20.0,
        legal_entity_id: str | None = None,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        self.cli_path = str(cli_path or root / ".tools" / "airwallex")
        self.journal_path = Path(journal_path or root / "finance" / "runs" / "airwallex.json")
        self.runner = runner or _subprocess_runner
        self.timeout = timeout
        self.legal_entity_id = legal_entity_id or os.getenv("AIRWALLEX_LEGAL_ENTITY_ID")

    def _argv(self, *parts: str, write: bool = False) -> tuple[str, ...]:
        operation = parts[:-1] if parts and parts[-1] == "--data-stdin" else parts
        allowed = operation in {
            ("auth", "whoami"),
            ("invoices", "create"),
            ("billing-customers", "create"),
            ("products", "create"),
        }
        allowed = allowed or (
            len(operation) == 3
            and operation[:2] in {
                ("invoices", "get"),
                ("billing-customers", "get"),
                ("products", "get"),
            }
            and bool(_SAFE_ID.fullmatch(operation[2]))
        )
        allowed = allowed or (
            len(operation) == 4
            and operation[:3]
            in {("invoices", "line-items", "add"), ("invoices", "line-items", "list")}
            and bool(_SAFE_ID.fullmatch(operation[3]))
        )
        if not allowed:
            raise AirwallexError("unsafe_command")
        argv = (self.cli_path, *parts, "--compact", "--no-telemetry")
        if write:
            argv += ("--confirm",)
        return argv

    async def _call(
        self, *parts: str, payload: Mapping[str, Any] | None = None, write: bool = False
    ) -> Any:
        operation = parts
        if payload is not None:
            parts = (*parts, "--data-stdin")
        argv = self._argv(*parts, write=write)
        stdin = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload else None

        async def invoke() -> Any:
            result = await self.runner(argv, stdin, self.timeout)
            if result.returncode != 0:
                raise AirwallexError(_classify_error(f"{result.stderr}\n{result.stdout}"))
            try:
                decoded = json.loads(result.stdout)
            except (TypeError, json.JSONDecodeError) as exc:
                raise AirwallexError("invalid_cli_response", uncertain=True) from exc
            if not isinstance(decoded, dict):
                raise AirwallexError("invalid_cli_response", uncertain=True)
            if decoded.get("ok") is False:
                raise AirwallexError(_classify_error(json.dumps(decoded.get("error", {}))))
            return decoded.get("data", decoded)

        trace_name = None
        if operation == ("invoices", "create"):
            trace_name = "airwallex-invoice-create"
        elif len(operation) == 3 and operation[:2] == ("invoices", "get"):
            trace_name = "airwallex-invoice-get"
        elif len(operation) == 4 and operation[:3] == ("invoices", "line-items", "add"):
            trace_name = "airwallex-line-items-add"
        elif len(operation) == 4 and operation[:3] == ("invoices", "line-items", "list"):
            trace_name = "airwallex-line-items-list"

        if trace_name is None:
            return await invoke()

        trace_input = {
            "operation": list(operation),
            "payload": sanitize_trace_data(payload) if payload is not None else None,
        }
        with trace_scope(trace_name, trace_input, as_type="tool") as span:
            decoded = await invoke()
            span.set_output(sanitize_trace_data(decoded))
            return decoded

    async def readiness(self) -> dict[str, Any]:
        try:
            identity = await self._call("auth", "whoami")
        except asyncio.TimeoutError:
            return {"ready": False, "authenticated": False, "environment": "unknown", "error": "timeout"}
        except AirwallexError as exc:
            return {
                "ready": False,
                "authenticated": False,
                "environment": "unknown",
                "error": exc.code,
            }
        if not isinstance(identity, dict):
            return {
                "ready": False,
                "authenticated": False,
                "environment": "unknown",
                "error": "invalid_cli_response",
            }
        authenticated = identity.get("is_authenticated") is True
        environment = str(identity.get("mode", "unknown")).lower()
        if not authenticated:
            return {
                "ready": False,
                "authenticated": False,
                "environment": environment,
                "error": "not_authenticated",
            }
        if environment != "sandbox" or identity.get("base_url") != SANDBOX_URL:
            return {
                "ready": False,
                "authenticated": True,
                "environment": environment,
                "error": "sandbox_required",
            }
        return {"ready": True, "authenticated": True, "environment": "sandbox", "error": None}

    def _load_journal(self) -> dict[str, Any]:
        try:
            data = json.loads(self.journal_path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise AirwallexError("invalid_journal") from exc

    def _save_journal(self, journal: Mapping[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.journal_path.with_suffix(self.journal_path.suffix + ".tmp")
        temporary.write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n")
        temporary.replace(self.journal_path)

    @staticmethod
    def _fingerprint(request: InvoiceRequest, draft: InvoiceDraft) -> str:
        canonical = json.dumps(
            {"request": request.model_dump(mode="json"), "draft": draft.model_dump(mode="json")},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _operation_id(request_id: str, fingerprint: str, operation: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"handoff:{request_id}:{fingerprint}:{operation}"))

    @staticmethod
    def _journal_entry(
        fingerprint: str, result: Mapping[str, Any], operation_ids: Mapping[str, str]
    ) -> dict[str, Any]:
        return {
            "fingerprint": fingerprint,
            "invoice_id": result.get("invoice_id"),
            "stage": result["stage"],
            "operation_ids": dict(operation_ids),
            "result": dict(result),
        }

    async def create_verified_invoice(
        self, request: InvoiceRequest, draft: InvoiceDraft
    ) -> dict[str, Any]:
        gate = validate_invoice(request, draft)
        if not gate.valid:
            return _result(
                "rejected",
                "validation",
                checks={check.rule: check.passed for check in gate.checks},
                error="core_validation_failed",
            )

        fingerprint = self._fingerprint(request, draft)
        async with _WRITE_LOCK:
            journal = self._load_journal()
            entries = journal.setdefault("invoices", {})
            existing = entries.get(request.request_id)
            if existing:
                if existing.get("fingerprint") != fingerprint:
                    return _result("conflict", "journal", error="request_content_changed")
                return dict(existing["result"])

            ready = await self.readiness()
            if not ready["ready"]:
                return _result("blocked", "readiness", error=ready["error"])

            create_operation_id = self._operation_id(
                request.request_id, fingerprint, "invoice-create"
            )
            line_operation_id = self._operation_id(
                request.request_id, fingerprint, "line-items-add"
            )
            operation_ids = {"invoice_create": create_operation_id, "line_items_add": line_operation_id}
            create_payload: dict[str, Any] = {
                "billing_customer_id": draft.customer_id,
                "currency": draft.currency,
                "request_id": create_operation_id,
                "collection_method": "OUT_OF_BAND",
                "days_until_due": draft.days_until_due,
                "default_tax_percent": _numeric(draft.tax_percent),
                "memo": "Draft created by Handoff finance demo.",
                "metadata": {
                    "handoff_reference": request.billing_reference,
                    "contract_id": draft.contract_id,
                    "fulfilment_id": draft.fulfilment_id,
                    "po_number": draft.po_number,
                },
            }
            if self.legal_entity_id:
                create_payload["legal_entity_id"] = _safe_id(self.legal_entity_id)

            in_progress = _result(
                "unknown", "create_uncertain", error="operation_in_progress"
            )
            entries[request.request_id] = self._journal_entry(
                fingerprint, in_progress, operation_ids
            )
            self._save_journal(journal)
            try:
                created = await self._call("invoices", "create", payload=create_payload, write=True)
            except asyncio.TimeoutError:
                result = _result("unknown", "create_uncertain", error="timeout")
                entries[request.request_id] = self._journal_entry(fingerprint, result, operation_ids)
                self._save_journal(journal)
                return result
            except AirwallexError as exc:
                result = _result(
                    "unknown" if exc.uncertain else "failed",
                    "create_uncertain" if exc.uncertain else "create_failed",
                    error=exc.code,
                )
                entries[request.request_id] = self._journal_entry(fingerprint, result, operation_ids)
                self._save_journal(journal)
                return result

            try:
                invoice_id = _safe_id(created.get("id") if isinstance(created, dict) else None)
            except AirwallexError:
                result = _result("unknown", "create_uncertain", error="missing_invoice_id")
                entries[request.request_id] = self._journal_entry(fingerprint, result, operation_ids)
                self._save_journal(journal)
                return result

            created_state = None
            if isinstance(created, dict):
                candidate_state = created.get("status") if "status" in created else created.get("state")
                created_state = candidate_state if isinstance(candidate_state, str) else None
            pending = _result(
                "partial",
                "line_items_uncertain",
                invoice_id=invoice_id,
                state=created_state,
                error="operation_in_progress",
            )
            entries[request.request_id] = self._journal_entry(fingerprint, pending, operation_ids)
            self._save_journal(journal)

            line_payload = {
                "request_id": line_operation_id,
                "line_items": [
                    {
                        "description": f"Implementation services — {request.billing_reference}",
                        "price": {
                            "pricing_model": "PER_UNIT",
                            "product_id": draft.product_id,
                            "unit_amount": _numeric(Decimal(draft.unit_price_minor) / Decimal(100)),
                            "tax_included": False,
                        },
                        "quantity": draft.quantity,
                        "tax_percent": _numeric(draft.tax_percent),
                        "metadata": {
                            "contract_id": draft.contract_id,
                            "fulfilment_id": draft.fulfilment_id,
                        },
                    }
                ],
            }
            try:
                await self._call(
                    "invoices", "line-items", "add", invoice_id, payload=line_payload, write=True
                )
            except asyncio.TimeoutError:
                result = _result(
                    "partial",
                    "line_items_uncertain",
                    invoice_id=invoice_id,
                    state=created_state,
                    error="timeout",
                )
                entries[request.request_id] = self._journal_entry(fingerprint, result, operation_ids)
                self._save_journal(journal)
                return result
            except AirwallexError as exc:
                result = _result(
                    "partial",
                    "line_items_uncertain" if exc.uncertain else "line_items_failed",
                    invoice_id=invoice_id,
                    state=created_state,
                    error=exc.code,
                )
                entries[request.request_id] = self._journal_entry(fingerprint, result, operation_ids)
                self._save_journal(journal)
                return result

            try:
                invoice = await self._call("invoices", "get", invoice_id)
                line_page = await self._call("invoices", "line-items", "list", invoice_id)
                checks = self._verification_checks(invoice, line_page, request, draft, gate.total_minor)
            except asyncio.TimeoutError:
                result = _result(
                    "partial",
                    "verification_failed",
                    invoice_id=invoice_id,
                    state=created_state,
                    error="timeout",
                )
            except AirwallexError as exc:
                result = _result(
                    "partial",
                    "verification_failed",
                    invoice_id=invoice_id,
                    state=created_state,
                    error=exc.code,
                )
            else:
                verified = all(checks.values())
                hosted_url = None
                if isinstance(invoice, dict):
                    candidate = invoice.get("hosted_invoice_url") or invoice.get("invoice_url")
                    if isinstance(candidate, str) and candidate.startswith("https://"):
                        hosted_url = candidate
                result = _result(
                    "success" if verified else "partial",
                    "verified" if verified else "verification_failed",
                    invoice_id=invoice_id,
                    state=(
                        invoice.get("status")
                        if isinstance(invoice, dict) and "status" in invoice
                        else invoice.get("state") if isinstance(invoice, dict) else None
                    ),
                    verified=verified,
                    checks=checks,
                    error=None if verified else "readback_mismatch",
                    hosted_url=hosted_url if verified else None,
                )
            entries[request.request_id] = self._journal_entry(fingerprint, result, operation_ids)
            self._save_journal(journal)
            return result

    @staticmethod
    def _verification_checks(
        invoice: Any,
        line_page: Any,
        request: InvoiceRequest,
        draft: InvoiceDraft,
        total_minor: int | None,
    ) -> dict[str, bool]:
        invoice = invoice if isinstance(invoice, dict) else {}
        lines = line_page.get("items") if isinstance(line_page, dict) else None
        lines = lines if isinstance(lines, list) else []
        line = lines[0] if len(lines) == 1 and isinstance(lines[0], dict) else {}
        price = line.get("price") if isinstance(line.get("price"), dict) else {}
        metadata = invoice.get("metadata") if isinstance(invoice.get("metadata"), dict) else {}
        line_metadata = line.get("metadata") if isinstance(line.get("metadata"), dict) else {}

        def decimal_equal(actual: Any, expected: Decimal) -> bool:
            if actual is None:
                return False
            try:
                return Decimal(str(actual)) == expected
            except Exception:
                return False

        expected_total = Decimal(total_minor) / Decimal(100) if total_minor is not None else Decimal("NaN")
        expected_unit = Decimal(draft.unit_price_minor) / Decimal(100)
        return {
            "state": (invoice.get("status") if "status" in invoice else invoice.get("state")) == "DRAFT",
            "collection_method": invoice.get("collection_method") == "OUT_OF_BAND",
            "billing_customer_id": invoice.get("billing_customer_id") == draft.customer_id,
            "currency": invoice.get("currency") == draft.currency,
            "days_until_due": invoice.get("days_until_due") == draft.days_until_due,
            "po_number": metadata.get("po_number") == draft.po_number,
            "total_amount": decimal_equal(invoice.get("total_amount"), expected_total),
            "line_count": len(lines) == 1,
            "line_product_id": price.get("product_id") == draft.product_id,
            "line_unit_amount": decimal_equal(price.get("unit_amount"), expected_unit),
            "line_quantity": line.get("quantity") == draft.quantity,
            "line_tax_percent": decimal_equal(line.get("tax_percent"), draft.tax_percent),
            "line_source_ids": line_metadata.get("contract_id") == draft.contract_id
            and line_metadata.get("fulfilment_id") == draft.fulfilment_id,
        }

    async def ensure_demo_objects(self) -> dict[str, str]:
        async with _WRITE_LOCK:
            journal = self._load_journal()
            receipt = journal.setdefault("demo_objects", {})
            operations = receipt.setdefault("operations", {})
            for resource in ("customer", "product"):
                operation = operations.get(resource, {})
                if not receipt.get(f"{resource}_id") and operation.get("stage") in {
                    "create_pending",
                    "create_uncertain",
                }:
                    raise AirwallexError(f"demo_{resource}_create_uncertain")

            ready = await self.readiness()
            if not ready["ready"]:
                raise AirwallexError(ready["error"] or "not_ready")

            customer_id = receipt.get("customer_id")
            if customer_id:
                customer_id = _safe_id(customer_id)
                customer = await self._call("billing-customers", "get", customer_id)
            else:
                customer, customer_id = await self._create_demo_resource(
                    journal,
                    receipt,
                    resource="customer",
                    command=("billing-customers", "create"),
                    payload={
                        "request_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "handoff-demo:customer")),
                        "name": "Handoff Demo Customer",
                        "default_billing_currency": "SGD",
                        "address": {"country_code": "SG"},
                        "metadata": {"handoff_demo": "true"},
                    },
                )
                customer = await self._call("billing-customers", "get", customer_id)
            if not self._valid_demo_customer(customer):
                raise AirwallexError("demo_customer_readback_mismatch")

            product_id = receipt.get("product_id")
            if product_id:
                product_id = _safe_id(product_id)
                product = await self._call("products", "get", product_id)
            else:
                product, product_id = await self._create_demo_resource(
                    journal,
                    receipt,
                    resource="product",
                    command=("products", "create"),
                    payload={
                        "request_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "handoff-demo:product")),
                        "name": "Handoff Demo Implementation Unit",
                        "description": "Synthetic implementation unit for the Handoff demo.",
                        "metadata": {"handoff_demo": "true"},
                    },
                )
                product = await self._call("products", "get", product_id)
            if not self._valid_demo_product(product):
                raise AirwallexError("demo_product_readback_mismatch")
            return {"customer_id": customer_id, "product_id": product_id}

    async def _create_demo_resource(
        self,
        journal: dict[str, Any],
        receipt: dict[str, Any],
        *,
        resource: str,
        command: tuple[str, str],
        payload: dict[str, Any],
    ) -> tuple[Any, str]:
        operations = receipt.setdefault("operations", {})
        operations[resource] = {
            "request_id": payload["request_id"],
            "stage": "create_pending",
        }
        self._save_journal(journal)
        try:
            created = await self._call(*command, payload=payload, write=True)
            resource_id = _safe_id(created.get("id") if isinstance(created, dict) else None)
        except asyncio.TimeoutError:
            operations[resource]["stage"] = "create_uncertain"
            operations[resource]["error"] = "timeout"
            self._save_journal(journal)
            raise AirwallexError(f"demo_{resource}_create_uncertain")
        except AirwallexError as exc:
            uncertain = exc.uncertain or exc.code == "unsafe_resource_id"
            operations[resource]["stage"] = (
                "create_uncertain" if uncertain else "create_failed"
            )
            operations[resource]["error"] = exc.code
            self._save_journal(journal)
            if uncertain:
                raise AirwallexError(f"demo_{resource}_create_uncertain") from exc
            raise
        receipt[f"{resource}_id"] = resource_id
        operations[resource]["stage"] = "created"
        self._save_journal(journal)
        return created, resource_id

    @staticmethod
    def _valid_demo_customer(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("name") == "Handoff Demo Customer"
            and value.get("default_billing_currency") == "SGD"
            and isinstance(value.get("address"), dict)
            and value["address"].get("country_code") == "SG"
            and isinstance(value.get("metadata"), dict)
            and value["metadata"].get("handoff_demo") == "true"
        )

    @staticmethod
    def _valid_demo_product(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("name") == "Handoff Demo Implementation Unit"
            and value.get("description") == "Synthetic implementation unit for the Handoff demo."
            and isinstance(value.get("metadata"), dict)
            and value["metadata"].get("handoff_demo") == "true"
        )


_default_client: AirwallexClient | None = None


def _client() -> AirwallexClient:
    global _default_client
    if _default_client is None:
        _default_client = AirwallexClient()
    return _default_client


async def create_verified_invoice(request: InvoiceRequest, draft: InvoiceDraft) -> dict[str, Any]:
    return await _client().create_verified_invoice(request, draft)


async def ensure_demo_objects() -> dict[str, str]:
    return await _client().ensure_demo_objects()


async def readiness() -> dict[str, Any]:
    return await _client().readiness()
