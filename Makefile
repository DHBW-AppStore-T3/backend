# ================================================================
# Backend Makefile — Test orchestration
# ================================================================
#
# This Makefile lives next to the backend service and complements the
# top-level deployment/Makefile. It runs the test suite inside the
# already-running ``backend-dev`` container so the Poetry environment
# (with pytest and all dev deps) is guaranteed to be available without
# the developer needing to ``poetry install`` on the host. The host
# generally has no pytest in PATH, which is why a bare ``pytest`` call
# falls over with ``command not found``.
#
# For host-side execution (if you ARE running Poetry locally), pass
# ``MODE=host``:
#   make test-backend-isolated MODE=host

.PHONY: help test-backend test-backend-isolated

.DEFAULT_GOAL := help

# ----------------------------------------------------------------
# Defaults for the isolated test database.
#
# Two address spaces matter:
#  - From INSIDE the docker-compose network the hostname is
#    ``postgres-test`` on port 5432. That is the path the
#    ``backend-dev`` container uses when ``MODE=container`` (default).
#  - From the HOST machine (or CI runners that run pytest natively)
#    the service is exposed at ``localhost:55433``. Use ``MODE=host``.
#
# Override anything on the command line:
#   make test-backend-isolated TEST_DB_HOST=otherhost
# ----------------------------------------------------------------
MODE ?= container

TEST_DB_USER     ?= postgres
TEST_DB_PASSWORD ?= postgres
TEST_DB_NAME     ?= backend_test

ifeq ($(MODE),host)
  TEST_DB_HOST     ?= localhost
  TEST_DB_PORT     ?= 55433
  PYTEST_RUNNER    := poetry run pytest
else
  TEST_DB_HOST     ?= postgres-test
  TEST_DB_PORT     ?= 5432
  # The container builds its venv at /app/.venv (see backend/Dockerfile.dev).
  # We invoke pytest through ``poetry run`` so the venv is activated even
  # though it's not on the default PATH — a bare ``pytest`` exec would
  # otherwise fail with ``executable file not found in $PATH``.
  PYTEST_RUNNER    := docker compose -f ../deployment/docker-compose.dev.yml exec -T backend poetry run pytest
endif

TEST_DATABASE_URL ?= postgresql://$(TEST_DB_USER):$(TEST_DB_PASSWORD)@$(TEST_DB_HOST):$(TEST_DB_PORT)/$(TEST_DB_NAME)

help: ## Show this help message
	@echo "Backend - Available Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Defaults: MODE=$(MODE), runs via: $(PYTEST_RUNNER)"
	@echo "Override MODE=host to run pytest on the host (poetry env)."

# ----------------------------------------------------------------
# Tests
# ----------------------------------------------------------------
test-backend: ## Run backend tests against the configured DATABASE_URL (legacy — may drop_all on dev DB)
	$(PYTEST_RUNNER)

test-backend-isolated: ## Run backend tests against the isolated postgres-test service (TEST_DATABASE_URL)
ifeq ($(MODE),host)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PYTEST_RUNNER)
else
	docker compose -f ../deployment/docker-compose.dev.yml exec -T \
	  -e TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
	  backend poetry run pytest
endif

# ----------------------------------------------------------------
# API Contract (OpenAPI)
# ----------------------------------------------------------------
openapi: ## Export OpenAPI specification to openapi.json
ifeq ($(MODE),host)
	python3 scripts/export_openapi.py openapi.json
else
	docker compose -f ../deployment/docker-compose.dev.yml exec -T backend python scripts/export_openapi.py openapi.json
endif

