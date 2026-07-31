"""Jinja2 dashboard pages.

The UI is deliberately thin: HTML tables, a multipart form and a small polling
script. The JSON APIs already carry everything the page needs, so the templates
are mostly layout.
"""

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.dependencies import SessionDep, SettingsDep
from app.core.enums import DocumentType
from app.services import job_service

router = APIRouter(tags=["dashboard"], include_in_schema=False)

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["pretty_json"] = lambda value: json.dumps(
    value, indent=2, default=str, sort_keys=True
)


@router.get("/", response_class=HTMLResponse, summary="Ops dashboard")
async def dashboard_home(request: Request, settings: SettingsDep) -> HTMLResponse:
    """Render the upload form, metrics strip and recent-jobs table shell."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "poll_interval_ms": settings.dashboard_poll_interval_ms,
            "recent_limit": settings.dashboard_recent_jobs_limit,
            "document_types": [member.value for member in DocumentType],
            "max_upload_mb": settings.max_upload_mb,
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse, summary="Job detail page")
async def dashboard_job_detail(
    request: Request, session: SessionDep, job_id: uuid.UUID
) -> HTMLResponse:
    """Render one job with its extraction payload pretty-printed."""
    job = await job_service.get_job(session, job_id)
    result_payload = None
    if job.result is not None:
        result_payload = {
            "detected_document_type": job.result.detected_document_type.value,
            "extracted_data": job.result.extracted_data,
            "model_provider": job.result.model_provider,
            "model_name": job.result.model_name,
            "prompt_version": job.result.prompt_version,
            "input_tokens": job.result.input_tokens,
            "output_tokens": job.result.output_tokens,
            "confidence_score": job.result.confidence_score,
        }
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "result_payload": result_payload,
            "filename": job.document.original_filename if job.document else None,
        },
    )
