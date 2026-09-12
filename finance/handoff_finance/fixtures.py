from __future__ import annotations

from datetime import date
from decimal import Decimal

from .models import Contract, Fulfilment, InvoiceDraft, InvoiceRequest


def _showcase() -> InvoiceRequest:
    old = Contract(
        id="contract-old",
        customer_id="customer-demo",
        product_id="implementation",
        currency="SGD",
        signed=True,
        effective_from=date(2026, 1, 1),
        effective_to=None,
        unit_price_minor=10_000,
        days_until_due=30,
        tax_percent=Decimal("9"),
        po_number="PO-2026-0912",
    )
    amendment = old.model_copy(
        update={
            "id": "contract-sep",
            "effective_from": date(2026, 9, 1),
            "unit_price_minor": 9_000,
        }
    )
    fulfilment = Fulfilment(
        id="fulfilment-sep",
        customer_id="customer-demo",
        product_id="implementation",
        accepted=True,
        accepted_quantity=8,
        previously_invoiced_quantity=2,
        accepted_on=date(2026, 9, 20),
    )
    proposed = InvoiceDraft(
        action="create_invoice",
        customer_id="customer-demo",
        product_id="implementation",
        currency="SGD",
        quantity=10,
        unit_price_minor=10_000,
        days_until_due=30,
        tax_percent=Decimal("9"),
        po_number="PO-2026-0912",
        contract_id="contract-old",
        fulfilment_id="fulfilment-sep",
        reason="Invoice the ordered quantity using the original agreement.",
    )
    return InvoiceRequest(
        request_id="showcase",
        objective="Create the invoice for accepted September implementation work.",
        billing_reference="BILL-2026-09",
        billing_date=date(2026, 9, 30),
        customer_id="customer-demo",
        product_id="implementation",
        currency="SGD",
        contracts=[old, amendment],
        fulfilment=fulfilment,
        existing_invoice_references=[],
        proposed=proposed,
    )


def cases() -> dict[str, InvoiceRequest]:
    showcase = _showcase()
    conflict = showcase.contracts[-1].model_copy(
        update={"id": "contract-sep-conflict", "unit_price_minor": 8_500}
    )
    return {
        "showcase": showcase,
        "conflicting_latest_contracts": showcase.model_copy(
            update={"request_id": "conflicting_latest_contracts", "contracts": [*showcase.contracts, conflict]}
        ),
        "unaccepted_fulfilment": showcase.model_copy(
            update={
                "request_id": "unaccepted_fulfilment",
                "fulfilment": showcase.fulfilment.model_copy(update={"accepted": False}),
            }
        ),
        "duplicate_billing_reference": showcase.model_copy(
            update={
                "request_id": "duplicate_billing_reference",
                "existing_invoice_references": [showcase.billing_reference],
            }
        ),
        "zero_billable_quantity": showcase.model_copy(
            update={
                "request_id": "zero_billable_quantity",
                "fulfilment": showcase.fulfilment.model_copy(
                    update={"previously_invoiced_quantity": showcase.fulfilment.accepted_quantity}
                ),
            }
        ),
        "missing_effective_contract": showcase.model_copy(
            update={"request_id": "missing_effective_contract", "contracts": []}
        ),
    }


def build_case(name: str = "showcase") -> InvoiceRequest:
    try:
        return cases()[name].model_copy(deep=True)
    except KeyError as exc:
        raise KeyError(f"unknown fixture case: {name}") from exc
