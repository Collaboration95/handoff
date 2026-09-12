from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import Check, Contract, GateResult, InvoiceDraft, InvoiceRequest


def _check(rule: str, actual: object, expected: object, evidence_ids: list[str]) -> Check:
    return Check(
        rule=rule,
        passed=actual == expected,
        expected=str(expected),
        actual=str(actual),
        evidence_ids=evidence_ids,
    )


def _effective_contract(request: InvoiceRequest) -> tuple[Contract | None, list[str]]:
    matches = [
        contract
        for contract in request.contracts
        if contract.signed
        and contract.customer_id == request.customer_id
        and contract.product_id == request.product_id
        and contract.currency == request.currency
        and contract.effective_from <= request.billing_date
        and (contract.effective_to is None or request.billing_date <= contract.effective_to)
    ]
    if not matches:
        return None, ["No signed matching contract is effective on the billing date."]

    latest_date = max(contract.effective_from for contract in matches)
    latest = [contract for contract in matches if contract.effective_from == latest_date]
    terms = {
        (
            contract.unit_price_minor,
            contract.days_until_due,
            contract.tax_percent,
            contract.po_number,
        )
        for contract in latest
    }
    if len(terms) > 1:
        return None, ["Conflicting contracts share the latest effective date."]
    return sorted(latest, key=lambda contract: contract.id)[0], []


def validate_invoice(request: InvoiceRequest, draft: InvoiceDraft) -> GateResult:
    """Validate a candidate solely against supplied structured source records."""
    contract, review_reasons = _effective_contract(request)
    fulfilment = request.fulfilment

    if not fulfilment.accepted:
        review_reasons.append("Fulfilment has not been accepted.")
    if fulfilment.customer_id != request.customer_id or fulfilment.product_id != request.product_id:
        review_reasons.append("Fulfilment does not belong to the requested customer and product.")
    if fulfilment.accepted_on > request.billing_date:
        review_reasons.append("Fulfilment acceptance is after the billing date.")
    if fulfilment.previously_invoiced_quantity > fulfilment.accepted_quantity:
        review_reasons.append("Previously invoiced quantity exceeds accepted quantity.")

    billable_quantity = fulfilment.accepted_quantity - fulfilment.previously_invoiced_quantity
    if billable_quantity == 0:
        review_reasons.append("No uninvoiced accepted quantity remains.")
    if request.billing_reference in request.existing_invoice_references:
        review_reasons.append("The billing reference already has an invoice.")

    checks: list[Check] = [
        _check("action", draft.action, "create_invoice", []),
        _check("customer", draft.customer_id, request.customer_id, [fulfilment.id]),
        _check("product", draft.product_id, request.product_id, [fulfilment.id]),
        _check("currency", draft.currency, request.currency, [contract.id] if contract else []),
        _check("quantity", draft.quantity, billable_quantity, [fulfilment.id]),
        _check("fulfilment_id", draft.fulfilment_id, fulfilment.id, [fulfilment.id]),
    ]
    if contract is not None:
        checks.extend(
            [
                _check("unit_price", draft.unit_price_minor, contract.unit_price_minor, [contract.id]),
                _check("days_until_due", draft.days_until_due, contract.days_until_due, [contract.id]),
                _check("tax_percent", draft.tax_percent, contract.tax_percent, [contract.id]),
                _check("po_number", draft.po_number, contract.po_number, [contract.id]),
                _check("contract_id", draft.contract_id, contract.id, [contract.id]),
            ]
        )

    valid = not review_reasons and all(check.passed for check in checks)
    if not valid:
        return GateResult(
            valid=False,
            checks=checks,
            review_reasons=review_reasons,
            subtotal_minor=None,
            tax_minor=None,
            total_minor=None,
        )

    subtotal = draft.quantity * draft.unit_price_minor
    tax = int(
        (Decimal(subtotal) * draft.tax_percent / Decimal(100)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return GateResult(
        valid=True,
        checks=checks,
        review_reasons=[],
        subtotal_minor=subtotal,
        tax_minor=tax,
        total_minor=subtotal + tax,
    )
