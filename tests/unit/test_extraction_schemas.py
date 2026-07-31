"""Typed extraction schemas: validation, registry selection, nullability."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.core.enums import DocumentType
from app.schemas.extraction import (
    SCHEMA_REGISTRY,
    GenericDocumentExtraction,
    InvoiceExtraction,
    InvoiceLineItem,
    ResumeExtraction,
    SupportTicketExtraction,
)


def test_registry_covers_every_document_type() -> None:
    assert set(SCHEMA_REGISTRY) == set(DocumentType)


@pytest.mark.parametrize(
    ("document_type", "schema"),
    list(SCHEMA_REGISTRY.items()),
)
def test_schema_selection_per_type(document_type: DocumentType, schema: type) -> None:
    assert SCHEMA_REGISTRY[document_type] is schema


def test_invoice_accepts_null_optional_fields() -> None:
    invoice = InvoiceExtraction(
        vendor_name=None,
        invoice_number=None,
        invoice_date=None,
        due_date=None,
        currency=None,
        subtotal=None,
        tax=None,
        total=None,
        line_items=[],
        confidence_score=0.5,
    )
    assert invoice.vendor_name is None
    assert invoice.line_items == []


def test_invoice_round_trips_populated_values() -> None:
    invoice = InvoiceExtraction(
        vendor_name="Northwind",
        invoice_number="INV-9",
        invoice_date=date(2026, 3, 1),
        due_date=date(2026, 3, 31),
        currency="EUR",
        subtotal=10.0,
        tax=2.0,
        total=12.0,
        line_items=[
            InvoiceLineItem(
                description="Widget",
                quantity=2.0,
                unit_price=5.0,
                amount=10.0,
            )
        ],
        confidence_score=0.9,
    )
    dumped = invoice.model_dump(mode="json")
    assert dumped["invoice_date"] == "2026-03-01"
    assert InvoiceExtraction.model_validate(dumped).total == 12.0


def test_optional_fields_are_required_but_nullable() -> None:
    """OpenAI strict mode needs every property in ``required``; defaults omit them."""
    schema = InvoiceExtraction.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert "default" not in schema["properties"]["vendor_name"]


def test_confidence_score_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        SupportTicketExtraction(
            subject=None,
            customer_name=None,
            customer_email=None,
            category=None,
            priority=None,
            summary=None,
            requested_action=None,
            sentiment=None,
            confidence_score=1.5,
        )


def test_resume_and_generic_require_list_fields() -> None:
    with pytest.raises(ValidationError):
        ResumeExtraction.model_validate({"confidence_score": 0.1})
    with pytest.raises(ValidationError):
        GenericDocumentExtraction.model_validate({"confidence_score": 0.1})


def test_malformed_payload_rejected() -> None:
    with pytest.raises(ValidationError):
        InvoiceExtraction.model_validate(
            {
                "vendor_name": "ACME",
                "invoice_number": "1",
                "invoice_date": None,
                "due_date": None,
                "currency": "USD",
                "subtotal": "not-a-number",
                "tax": None,
                "total": None,
                "line_items": [],
                "confidence_score": 0.2,
            }
        )
