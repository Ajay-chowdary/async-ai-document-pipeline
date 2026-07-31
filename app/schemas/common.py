"""Response envelopes shared across routers."""

from pydantic import BaseModel, Field

#: Pagination bounds, enforced by the API so a client cannot request the whole
#: table in one query.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class Page[ItemT](BaseModel):
    """One page of results plus enough context to request the next."""

    items: list[ItemT]
    total: int = Field(description="Total rows matching the filter, ignoring pagination.")
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        """Whether further rows exist beyond this page."""
        return self.offset + len(self.items) < self.total


class ErrorDetail(BaseModel):
    """The body of an error response."""

    code: str
    message: str
    details: dict[str, object] | None = None
    correlation_id: str | None = None


class ErrorResponse(BaseModel):
    """Every non-2xx response from this API has this shape."""

    error: ErrorDetail
