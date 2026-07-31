"""The LLM provider interface.

Kept narrow on purpose: two operations, both returning plain data. Anything a
particular vendor's SDK does beyond that stays behind the implementation, so
swapping OpenAI for Anthropic is a new file rather than a change to the worker.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.core.enums import DocumentType
from app.schemas.extraction import SCHEMA_REGISTRY


@dataclass(frozen=True, slots=True)
class ExtractionOutput:
    """One extraction, plus the provenance needed to interpret it later.

    Token counts are optional because not every provider reports them, and a
    missing count should read as "unknown", never as zero.
    """

    document_type: DocumentType
    data: dict[str, Any]
    model_provider: str
    model_name: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: Self-reported by the model, 0..1. Not a calibrated probability.
    confidence_score: float | None = None

    @property
    def total_tokens(self) -> int | None:
        """Combined token usage, or ``None`` if either side is unknown."""
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ClassificationOutput:
    """A document-type decision and what it cost."""

    document_type: DocumentType
    input_tokens: int | None = None
    output_tokens: int | None = None
    confidence_score: float | None = None


class LLMProvider(ABC):
    """What the worker requires of a language-model backend."""

    #: Identifies the backend in ``extraction_results.model_provider``.
    name: str = "abstract"

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The specific model in use, recorded alongside every result."""

    @abstractmethod
    async def classify(self, text: str) -> ClassificationOutput:
        """Decide which of the supported types a document is."""

    @abstractmethod
    async def extract(self, text: str, document_type: DocumentType) -> ExtractionOutput:
        """Extract the structured record for a known document type."""

    async def aclose(self) -> None:
        """Release any held resources. Overridden by providers that need it."""
        return None


@dataclass
class FakeLLMProvider(LLMProvider):
    """A deterministic in-process provider.

    Exists so the entire pipeline — queue, worker, retries, persistence — can be
    exercised in tests and by ``LLM_PROVIDER=fake`` without an API key, network
    access or spend. Determinism matters: a test asserting on extraction output
    must not depend on a model's mood.

    ``failures`` lets a test make the provider fail on demand, which is how the
    retry and dead-letter paths are covered.

    Payloads are validated against the same schemas the OpenAI provider uses, so
    a schema change that would break production fails the fake path too.
    """

    name: str = "fake"
    _model_name: str = "fake-extractor-1"
    prompt_version: str = "fake-v1"
    #: Popped one per call; raise these to simulate provider failures.
    failures: list[Exception] = field(default_factory=list)
    calls: int = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)

    async def classify(self, text: str) -> ClassificationOutput:
        """Guess a type from obvious keywords, defaulting to generic."""
        self._maybe_fail()
        return ClassificationOutput(
            document_type=_guess_type(text),
            input_tokens=len(text) // 4,
            output_tokens=4,
            confidence_score=0.9,
        )

    async def extract(self, text: str, document_type: DocumentType) -> ExtractionOutput:
        """Return a schema-valid payload for the requested document type."""
        self._maybe_fail()
        schema = SCHEMA_REGISTRY[document_type]
        payload = schema.model_validate(_fake_payload(document_type, text)).model_dump(mode="json")
        confidence = payload["confidence_score"]
        return ExtractionOutput(
            document_type=document_type,
            data=payload,
            model_provider=self.name,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            input_tokens=len(text) // 4,
            output_tokens=32,
            confidence_score=float(confidence),
        )


#: Keywords good enough for the fake provider. The real classifier is a model
#: call; this only needs to be deterministic and roughly sensible.
_TYPE_HINTS: tuple[tuple[DocumentType, tuple[str, ...]], ...] = (
    (DocumentType.INVOICE, ("invoice", "vendor", "amount due", "subtotal")),
    (DocumentType.RESUME, ("resume", "curriculum vitae", "work experience", "education")),
    (DocumentType.SUPPORT_TICKET, ("ticket", "support request", "customer reported")),
)


def _guess_type(text: str) -> DocumentType:
    lowered = text.lower()
    for document_type, hints in _TYPE_HINTS:
        if any(hint in lowered for hint in hints):
            return document_type
    return DocumentType.GENERIC


def _fake_payload(document_type: DocumentType, text: str) -> dict[str, Any]:
    """Build a stable, schema-valid dict for the given type.

    A few fields are derived from the input text so end-to-end demos look
    plausible; everything else is fixed so tests stay deterministic.
    """
    snippet = " ".join(text.split())[:200] or None
    if document_type is DocumentType.INVOICE:
        return {
            "vendor_name": _find_after(text, "vendor:") or "Example Vendor",
            "invoice_number": "INV-1001",
            "invoice_date": date(2026, 1, 15),
            "due_date": date(2026, 2, 15),
            "currency": "USD",
            "subtotal": 100.0,
            "tax": 8.0,
            "total": 108.0,
            "line_items": [
                {
                    "description": "Professional services",
                    "quantity": 1.0,
                    "unit_price": 100.0,
                    "amount": 100.0,
                }
            ],
            "confidence_score": 0.75,
        }
    if document_type is DocumentType.RESUME:
        return {
            "candidate_name": "Alex Example",
            "email": "alex@example.com",
            "phone": None,
            "location": None,
            "summary": snippet,
            "skills": ["python", "sql"],
            "education": [
                {
                    "institution": "Example University",
                    "degree": "BSc",
                    "field_of_study": "Computer Science",
                    "start_date": "2018",
                    "end_date": "2022",
                }
            ],
            "experience": [
                {
                    "company": "Example Corp",
                    "title": "Engineer",
                    "start_date": "2022",
                    "end_date": None,
                    "description": snippet,
                }
            ],
            "total_years_of_experience": 3.0,
            "confidence_score": 0.75,
        }
    if document_type is DocumentType.SUPPORT_TICKET:
        return {
            "subject": snippet,
            "customer_name": "Casey Customer",
            "customer_email": "casey@example.com",
            "category": "billing",
            "priority": "medium",
            "summary": snippet,
            "requested_action": "Investigate and reply",
            "sentiment": "neutral",
            "confidence_score": 0.75,
        }
    return {
        "title": snippet,
        "document_type": "memo",
        "summary": snippet,
        "key_entities": ["Example Org"],
        "important_dates": [],
        "action_items": [],
        "confidence_score": 0.75,
    }


def _find_after(text: str, marker: str) -> str | None:
    """Return the remainder of the line after ``marker``, if present."""
    lowered = text.lower()
    index = lowered.find(marker.lower())
    if index < 0:
        return None
    remainder = text[index + len(marker) :].splitlines()[0].strip()
    return remainder or None
