"""Extraction result envelope and the four typed LLM output schemas.

Each schema is also the JSON Schema sent to the model under structured
outputs. OpenAI's ``strict: true`` mode requires every property to appear in
``required``, so optional values are declared as ``T | None`` with no default
— required-but-nullable — and the prompts ask the model to return ``null``
rather than omit the field.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import DocumentType


class ExtractionResultResponse(BaseModel):
    """Extraction output plus the provenance needed to interpret it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    detected_document_type: DocumentType
    extracted_data: dict[str, Any] = Field(
        description="Validated extraction payload; its shape follows the detected type."
    )
    model_provider: str
    model_name: str
    prompt_version: str = Field(
        description="Identifies the prompt that produced this result, for quality comparison."
    )
    input_tokens: int | None
    output_tokens: int | None
    confidence_score: float | None = Field(
        default=None,
        description=(
            "Self-reported by the model, 0..1. A hint about extraction quality, "
            "not a calibrated probability."
        ),
    )
    created_at: datetime


class InvoiceLineItem(BaseModel):
    """One row on an invoice."""

    description: str | None
    quantity: float | None
    unit_price: float | None
    amount: float | None


class InvoiceExtraction(BaseModel):
    """Structured fields extracted from an invoice."""

    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str | None
    subtotal: float | None
    tax: float | None
    total: float | None
    line_items: list[InvoiceLineItem]
    confidence_score: float = Field(ge=0.0, le=1.0)


class EducationEntry(BaseModel):
    """One education row on a resume."""

    institution: str | None
    degree: str | None
    field_of_study: str | None
    start_date: str | None
    end_date: str | None


class ExperienceEntry(BaseModel):
    """One employment row on a resume."""

    company: str | None
    title: str | None
    start_date: str | None
    end_date: str | None
    description: str | None


class ResumeExtraction(BaseModel):
    """Structured fields extracted from a resume."""

    candidate_name: str | None
    email: str | None
    phone: str | None
    location: str | None
    summary: str | None
    skills: list[str]
    education: list[EducationEntry]
    experience: list[ExperienceEntry]
    total_years_of_experience: float | None
    confidence_score: float = Field(ge=0.0, le=1.0)


class SupportTicketExtraction(BaseModel):
    """Structured fields extracted from a support ticket."""

    subject: str | None
    customer_name: str | None
    customer_email: str | None
    category: str | None
    priority: str | None
    summary: str | None
    requested_action: str | None
    sentiment: str | None
    confidence_score: float = Field(ge=0.0, le=1.0)


class GenericDocumentExtraction(BaseModel):
    """Structured fields extracted from an unclassified document."""

    title: str | None
    document_type: str | None
    summary: str | None
    key_entities: list[str]
    important_dates: list[str]
    action_items: list[str]
    confidence_score: float = Field(ge=0.0, le=1.0)


class DocumentClassification(BaseModel):
    """Which of the four supported types a document is."""

    document_type: DocumentType
    confidence_score: float = Field(ge=0.0, le=1.0)


SCHEMA_REGISTRY: dict[DocumentType, type[BaseModel]] = {
    DocumentType.INVOICE: InvoiceExtraction,
    DocumentType.RESUME: ResumeExtraction,
    DocumentType.SUPPORT_TICKET: SupportTicketExtraction,
    DocumentType.GENERIC: GenericDocumentExtraction,
}
