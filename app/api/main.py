"""FastAPI application factory, middleware and error handling."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_422_UNPROCESSABLE_CONTENT, HTTP_500_INTERNAL_SERVER_ERROR

from app import __version__
from app.api.routes import dashboard, documents, health, jobs, metrics
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.core.logging import (
    bind_correlation_id,
    clear_context,
    configure_logging,
    get_correlation_id,
    get_logger,
)
from app.core.time import utcnow
from app.db.session import dispose_engine, wait_for_database
from app.services.file_storage import get_file_storage
from app.services.queue import get_queue

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = get_logger(__name__)

#: Echoed on every response and accepted on every request, so a client can tie
#: its own trace to the API and worker log lines for the same document.
CORRELATION_HEADER = "X-Correlation-ID"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare dependencies on startup and release them on shutdown.

    The database is waited for rather than assumed: under Compose the API
    container frequently wins the race against PostgreSQL, and retrying here is
    more robust than any ordering directive or fixed sleep.
    """
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    logger.info(
        "api.starting",
        version=__version__,
        environment=settings.environment,
        llm_provider=settings.llm_provider.value,
    )

    get_file_storage(settings)
    await wait_for_database(settings)

    queue = get_queue(settings)
    await queue.connect()
    # The API creates the group too, so a worker starting later joins an
    # existing one and uploads are never published into a stream nobody reads.
    await queue.ensure_group()

    logger.info("api.ready")
    try:
        yield
    finally:
        await queue.close()
        await dispose_engine()
        logger.info("api.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application."""
    resolved = settings or get_settings()

    app = FastAPI(
        title="Async AI Document Processing Pipeline",
        version=__version__,
        description=(
            "Upload documents for asynchronous, LLM-based structured extraction. "
            "Uploads return 202 immediately; poll the job for the result."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            expose_headers=[CORRELATION_HEADER],
        )

    _register_middleware(app)
    _register_exception_handlers(app)

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)
    app.include_router(dashboard.router)

    return app


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_and_access_log(
        request: Request, call_next: Callable[[Request], Awaitable[JSONResponse]]
    ) -> JSONResponse:
        """Bind a correlation ID for the request and log its outcome."""
        clear_context()
        correlation_id = bind_correlation_id(request.headers.get(CORRELATION_HEADER))
        started = utcnow()

        try:
            response = await call_next(request)
        finally:
            duration_ms = int((utcnow() - started).total_seconds() * 1000)

        response.headers[CORRELATION_HEADER] = correlation_id
        logger.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        clear_context()
        return response


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, error: AppError) -> JSONResponse:
        """Render a deliberate application error using its own status and code."""
        log = logger.warning if error.http_status < HTTP_500_INTERNAL_SERVER_ERROR else logger.error
        log("api.error", code=error.code, status_code=error.http_status, message=error.message)
        return JSONResponse(
            status_code=error.http_status,
            content=error.to_dict(correlation_id=get_correlation_id()),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """Render FastAPI's validation failures in the standard envelope."""
        fields = [
            {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "request_validation_failed",
                    "message": "The request payload failed validation.",
                    "details": {"fields": fields},
                    "correlation_id": get_correlation_id(),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        """Keep framework-raised errors, such as unknown routes, in one shape."""
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": "http_error",
                    "message": str(error.detail),
                    "correlation_id": get_correlation_id(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        """Log the real cause, return a message that reveals nothing internal.

        The correlation ID is the bridge: the client quotes it, and the
        operator finds the stack trace in the logs.
        """
        logger.exception("api.unhandled_error", error_type=type(error).__name__)
        return JSONResponse(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred.",
                    "correlation_id": get_correlation_id(),
                }
            },
        )


app = create_app()
