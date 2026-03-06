# CDC Simulator — developer task runner
#
# Run `make help` to see all available commands.
# Run `make setup` once after cloning the repo to create the virtual environment.
#
# All commands use the .venv inside this project. You do not need to activate
# the virtual environment manually — the Makefile handles it.

.DEFAULT_GOAL := help

PYTHON     := .venv/bin/python
PIP        := .venv/bin/pip
PYTEST     := .venv/bin/pytest
RUFF       := .venv/bin/ruff
MYPY       := .venv/bin/mypy

# Detect if the virtual environment exists
VENV_EXISTS := $(shell test -d .venv && echo "yes" || echo "no")

# ── Environment selector ───────────────────────────────────────────────────────
#
# Controls where the simulator connects. Defaults to local Docker.
#
# Usage:
#   make schema                   connects to local Docker (reads .env)
#   make schema ENV=dev           connects to AWS dev RDS   (password from SSM)
#   make schema ENV=staging       connects to AWS staging   (password from SSM)
#   make schema ENV=prod          connects to AWS prod      (password from SSM)
#
# AWS usage requires:
#   1. SSM tunnel open in a separate terminal (see cloud_setup_guide.md)
#   2. AWS profile named dev-admin / staging-admin / prod-admin configured
#   3. make apply dev/staging/prod already run so the SSM parameter exists

ENV ?= local

ifeq ($(ENV),local)
# Local Docker: source .env file
RUN := set -a && . .env && set +a &&
else
# AWS: all static vars inline, password fetched live from SSM Parameter Store.
# No password file ever exists on disk.
RUN := \
  export DB_HOST=localhost && \
  export DB_PORT=5433 && \
  export DB_NAME=ecommerce && \
  export DB_USER=postgres && \
  export DB_PASSWORD=$$(aws ssm get-parameter \
    --name /edp/$(ENV)/rds/db_password \
    --with-decryption \
    --query Parameter.Value \
    --output text \
    --profile $(ENV)-admin) && \
  export ENVIRONMENT=$(ENV) && \
  export TEST_DB_NAME=ecommerce_test && \
  export SEED_RANDOM_SEED=42 && \
  export SIM_TICK_INTERVAL_SECONDS=2 && \
  export SIM_NEW_ORDERS_PER_TICK=3 && \
  export RETRY_MAX_ATTEMPTS=5 && \
  export RETRY_WAIT_MIN_SECONDS=1 && \
  export RETRY_WAIT_MAX_SECONDS=30 &&
endif

.PHONY: help setup lint typecheck test test-unit test-integration \
        schema seed simulate reset \
        docker-build docker-up docker-down docker-logs docker-simulate \
        clean

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "CDC Simulator — available commands:"
	@echo ""
	@echo "  Setup"
	@echo "    make setup              Create .venv and install all dependencies"
	@echo ""
	@echo "  Code quality"
	@echo "    make lint               Run ruff linter"
	@echo "    make typecheck          Run mypy type checker"
	@echo "    make test               Run all tests (unit + integration)"
	@echo "    make test-unit          Run unit tests only (no database required)"
	@echo "    make test-integration   Run integration tests (requires ENVIRONMENT + DB vars)"
	@echo ""
	@echo "  Simulator"
	@echo "    make schema             Create tables (local Docker)"
	@echo "    make schema ENV=dev     Create tables (AWS dev RDS)"
	@echo "    make schema ENV=staging Create tables (AWS staging RDS)"
	@echo "    make schema ENV=prod    Create tables (AWS prod RDS)"
	@echo "    make seed               Seed historical data  (add ENV=dev/staging/prod for AWS)"
	@echo "    make simulate           Run the live simulation loop  (add ENV=dev/staging/prod for AWS)"
	@echo "    make reset              Drop all tables, recreate schema, reseed  (supports ENV=)"
	@echo ""
	@echo "  Docker"
	@echo "    make docker-up          Start local PostgreSQL in Docker"
	@echo "    make docker-down        Stop and remove Docker containers"
	@echo "    make docker-logs        Tail Docker container logs"
	@echo "    make docker-build       Build the simulator Docker image"
	@echo "    make docker-simulate    Run the full stack (Postgres + simulator) in Docker"
	@echo ""
	@echo "  Cleanup"
	@echo "    make clean              Remove .venv and Python cache files"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────

setup:
	@echo "Creating virtual environment with Python 3.11.8..."
	python -m venv .venv
	$(PIP) install --upgrade pip --quiet
	$(PIP) install -r requirements.txt -r requirements-dev.txt --quiet
	@echo ""
	@echo "Setup complete."
	@echo "Next: copy .env.example to .env and fill in your local Docker credentials."
	@echo "For AWS: run 'make apply dev/staging/prod' first, then use ENV=dev/staging/prod."
	@echo "Then run: make docker-up && make schema && make seed && make simulate"

# ── Code quality ──────────────────────────────────────────────────────────────

lint:
	@echo "Running ruff..."
	$(RUFF) check simulator/ main.py tests/

typecheck:
	@echo "Running mypy..."
	$(MYPY) simulator/ main.py --ignore-missing-imports

test:
	@echo "Running all tests..."
	$(RUN) $(PYTEST) tests/ --cov=simulator --cov-report=term-missing

test-unit:
	@echo "Running unit tests (no database required)..."
	$(PYTEST) tests/ -m "not integration" --cov=simulator --cov-report=term-missing

test-integration:
	@echo "Running integration tests (requires a running PostgreSQL)..."
	$(RUN) $(PYTEST) tests/ -m integration -v

# ── Simulator commands ────────────────────────────────────────────────────────
# Local: reads .env — run make docker-up first.
# AWS:   add ENV=dev/staging/prod — fetches password from SSM automatically.

schema:
	$(RUN) $(PYTHON) main.py schema

seed:
	$(RUN) $(PYTHON) main.py seed

simulate:
	$(RUN) $(PYTHON) main.py simulate

reset:
	$(RUN) $(PYTHON) main.py reset

# ── Docker ────────────────────────────────────────────────────────────────────

docker-up:
	@echo "Starting local PostgreSQL..."
	docker compose up -d postgres
	@echo "Waiting for PostgreSQL to be ready..."
	@$(RUN) docker compose exec postgres sh -c "until pg_isready -U \$${DB_USER:-postgres}; do sleep 1; done"
	@echo "PostgreSQL is ready."

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-build:
	@echo "Building simulator Docker image..."
	docker build -t cdc-simulator:latest .

docker-simulate:
	@echo "Starting full stack (PostgreSQL + simulator) in Docker..."
	docker compose up --build

# ── Cleanup ───────────────────────────────────────────────────────────────────

clean:
	rm -rf .venv
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned."
