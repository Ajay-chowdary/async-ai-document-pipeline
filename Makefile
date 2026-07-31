.DEFAULT_GOAL := help
SHELL := /bin/bash

VENV       := .venv
PY         := $(VENV)/bin/python
PYTEST     := $(VENV)/bin/pytest
RUFF       := $(VENV)/bin/ruff
MYPY       := $(VENV)/bin/mypy
ALEMBIC    := $(VENV)/bin/alembic
UVICORN    := $(VENV)/bin/uvicorn
COMPOSE    := docker compose

# Integration tests need a real PostgreSQL. Override when your local server
# uses different credentials, e.g.
#   make test TEST_DATABASE_URL=postgresql+asyncpg://me@localhost:5432/docpipeline_test
# Tests that need it skip automatically when nothing is listening.
TEST_DATABASE_URL ?= postgresql+asyncpg://postgres:postgres@localhost:5432/docpipeline_test
TEST_REDIS_URL ?= redis://localhost:6379/15
export TEST_DATABASE_URL
export TEST_REDIS_URL

.PHONY: help setup up down logs ps migrate migration test test-unit test-integration \
        lint format typecheck check api worker seed benchmark shell clean db-create redis

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Local environment -------------------------------------------------------

setup: ## Create the virtualenv, install dependencies, copy .env
	uv venv --python 3.12 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

# --- Docker ------------------------------------------------------------------

up: ## Start the full stack (postgres, redis, migrations, api, worker)
	$(COMPOSE) up -d --build

down: ## Stop the stack, keeping volumes
	$(COMPOSE) down

logs: ## Follow logs from all services
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

# --- Database ----------------------------------------------------------------

migrate: ## Apply all pending Alembic migrations
	$(ALEMBIC) upgrade head

migration: ## Autogenerate a migration: make migration m="add foo"
	$(ALEMBIC) revision --autogenerate -m "$(m)"

# --- Quality -----------------------------------------------------------------

test: ## Run the full test suite
	$(PYTEST)

test-unit: ## Run unit tests only (no external services required)
	$(PYTEST) tests/unit

db-create: ## Create the local dev and test databases
	createdb docpipeline 2>/dev/null || true
	createdb docpipeline_test 2>/dev/null || true

test-integration: ## Run integration tests (requires postgres and redis)
	$(PYTEST) tests/integration -m integration

redis: ## Start a local Redis in the background (dev convenience)
	redis-server --daemonize yes --save "" --appendonly no

lint: ## Check formatting and lint rules
	$(RUFF) check .
	$(RUFF) format --check .

format: ## Apply formatting and safe lint fixes
	$(RUFF) format .
	$(RUFF) check --fix .

typecheck: ## Run mypy
	$(MYPY) app

check: lint typecheck test ## Run every quality gate

# --- Processes ---------------------------------------------------------------

api: ## Run the API with autoreload
	$(UVICORN) app.api.main:app --reload --host 127.0.0.1 --port 8000

worker: ## Run a single worker process
	$(PY) -m app.worker.main

# --- Demo --------------------------------------------------------------------

seed: ## Upload the synthetic sample documents and print the job IDs
	$(PY) scripts/seed.py

benchmark: ## Measure end-to-end latency: make benchmark n=20
	$(PY) scripts/benchmark.py --count $${n:-10}

shell: ## Open a Python shell with the app importable
	$(PY)

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
