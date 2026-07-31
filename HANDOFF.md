# Handoff — complete

All eight phases are implemented and verified. This file is retained as
historical context for conventions and known gaps; prefer the README for
day-to-day use.

---

## 1. Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Scaffold, typed settings, exceptions, structured logging | Done |
| 2 | Models, Alembic migration, storage, upload + read APIs | Done |
| 3 | Redis Streams, worker, retries, recovery, retry endpoint | Done |
| 4 | Extraction schemas, prompts, OpenAI provider | Done |
| 5 | Dashboard, `/metrics-summary`, benchmark script | Done |
| 6 | Dockerfiles, docker-compose | Done |
| 7 | Terraform (single EC2) | Done |
| 8 | Final quality pass, README completion | Done |

Run the gates with:

```bash
make check TEST_DATABASE_URL=postgresql+asyncpg://apple@localhost:5432/docpipeline_test
```

(Adjust the URL for your local PostgreSQL user.)

---

## 2. Local environment (this machine)

PostgreSQL runs as user `apple` with no password. Redis may need:

```bash
make redis
```

Python 3.12 venv is at `.venv` (created with `uv`). Databases `docpipeline` and
`docpipeline_test` should already exist.

---

## 3. Conventions

- No TODOs for core functionality. Raise an explicit typed error instead.
- No invented benchmark numbers in the README.
- No claims of production readiness for the single-EC2 demo.
- Type annotations everywhere; `mypy --strict` on `app/`.
- Timezone-aware UTC via `app.core.time.utcnow()`.
- Configuration only through `app/core/config.py`.
- Structured logging via `app.core.logging.get_logger()`; never log document
  contents or API keys.
- Tests must pass with no API key and no network; `tests/conftest.py` clears
  `OPENAI_API_KEY`.
- Integration tests skip cleanly when PostgreSQL or Redis is absent.

---

## 4. Architecture facts you must not break

`job_service.claim_job` is the idempotency barrier. `XACK` happens only after
the PostgreSQL transaction commits. Retries are scheduled in a Redis sorted
set. `session.rollback()` expires ORM objects — refresh after rollback.
Relationship cascades are spelled out without `all`. Redis socket timeout must
exceed `REDIS_BLOCK_MS`.

---

## 5. Known gaps (do not hide)

1. Upload path is not transactional across PostgreSQL and Redis (outbox would
   be the production fix).
2. A retry popped from the delayed set that then fails to publish is lost.
3. `confidence_score` is self-reported and not calibrated.
4. No OCR — image-only PDFs fail permanently.
5. No authentication on the API or dashboard.
6. One event in flight per worker process.
