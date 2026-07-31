"""Versioned prompts for classification and extraction.

``PROMPT_VERSION`` is persisted on every result so two extractions of the same
document can be compared when the wording changes. Bump it when the instructions
change in a way that would alter output quality.
"""

from typing import Any

from app.core.enums import DocumentType

PROMPT_VERSION = "v1"

_EXTRACT_SYSTEM = """\
You extract structured data from documents.

Rules:
- Extract only facts the document supports.
- Return null for any field that is absent or unclear. Never invent values.
- Return every field in the schema; use null rather than omitting a key.
- confidence_score is your self-reported confidence on a 0 to 1 scale. It is a \
hint, not a calibrated probability.
"""

_CLASSIFY_SYSTEM = """\
You classify a document into exactly one of these types: invoice, resume, \
support_ticket, generic.

Rules:
- Choose the single best match from the document content.
- Use generic when none of the other types clearly apply.
- confidence_score is your self-reported confidence on a 0 to 1 scale.
"""

_TYPE_HINTS: dict[DocumentType, str] = {
    DocumentType.INVOICE: (
        "This document is an invoice. Extract vendor, amounts, dates and line items."
    ),
    DocumentType.RESUME: (
        "This document is a resume or CV. Extract the candidate, skills, education and experience."
    ),
    DocumentType.SUPPORT_TICKET: (
        "This document is a support ticket. Extract the subject, customer, "
        "priority, sentiment and requested action."
    ),
    DocumentType.GENERIC: (
        "Extract a title, short summary, key entities, important dates and action items."
    ),
}


def classification_messages(text: str) -> list[dict[str, Any]]:
    """Messages for a cheap type decision over a short sample of the document."""
    return [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {
            "role": "user",
            "content": f"Classify the following document.\n\n---\n{text}\n---",
        },
    ]


def extraction_messages(text: str, document_type: DocumentType) -> list[dict[str, Any]]:
    """Messages for structured extraction against the selected schema."""
    hint = _TYPE_HINTS[document_type]
    return [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {
            "role": "user",
            "content": (f"{hint}\n\nDocument type: {document_type.value}\n\n---\n{text}\n---"),
        },
    ]


def repair_user_message() -> str:
    """Follow-up when the first structured response failed validation."""
    return (
        "Your previous response did not satisfy the schema. Return a corrected "
        "JSON object that matches the schema exactly. Use null for missing "
        "fields. Do not invent values."
    )
