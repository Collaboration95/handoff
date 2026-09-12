from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Contract(FrozenModel):
    id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    currency: Literal["SGD", "USD"]
    signed: StrictBool
    effective_from: date
    effective_to: date | None = None
    unit_price_minor: StrictInt = Field(ge=0)
    days_until_due: StrictInt = Field(ge=1, le=365)
    tax_percent: Decimal = Field(ge=0, le=100)
    po_number: str = Field(min_length=1)

    @field_validator("tax_percent")
    @classmethod
    def finite_tax(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("tax_percent must be finite")
        return value


class Fulfilment(FrozenModel):
    id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    accepted: StrictBool
    accepted_quantity: StrictInt = Field(ge=0)
    previously_invoiced_quantity: StrictInt = Field(ge=0)
    accepted_on: date


class InvoiceDraft(FrozenModel):
    action: Literal["create_invoice", "request_review"]
    customer_id: str
    product_id: str
    currency: Literal["SGD", "USD"]
    quantity: StrictInt = Field(ge=0)
    unit_price_minor: StrictInt = Field(ge=0)
    days_until_due: StrictInt = Field(ge=1, le=365)
    tax_percent: Decimal = Field(ge=0, le=100)
    po_number: str
    contract_id: str
    fulfilment_id: str
    reason: str

    @field_validator("tax_percent")
    @classmethod
    def finite_tax(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("tax_percent must be finite")
        return value


class InvoiceRequest(FrozenModel):
    request_id: str = Field(min_length=1)
    objective: str
    billing_reference: str = Field(min_length=1)
    billing_date: date
    customer_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    currency: Literal["SGD", "USD"]
    contracts: list[Contract]
    fulfilment: Fulfilment
    existing_invoice_references: list[str]
    proposed: InvoiceDraft | None = None


class Check(FrozenModel):
    rule: str
    passed: bool
    expected: str
    actual: str
    evidence_ids: list[str]


class GateResult(FrozenModel):
    valid: bool
    checks: list[Check]
    review_reasons: list[str]
    subtotal_minor: int | None
    tax_minor: int | None
    total_minor: int | None


class Decision(FrozenModel):
    request_id: str
    objective: str
    status: Literal["accepted", "repaired", "needs_review", "error"]
    baseline: InvoiceDraft | None
    baseline_checks: GateResult | None
    selected: InvoiceDraft | None
    candidates: list[InvoiceDraft]
    candidate_checks: list[GateResult]
    trace_id: str | None = None
    trace_url: str | None = None
    elapsed_ms: int
    usage: dict
    error: str | None
