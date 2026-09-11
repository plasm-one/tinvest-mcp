.DEFAULT_GOAL := help
.PHONY: help install dev test test-cov lint fmt check doctor run smoke debug-api docker clean

UV := uv

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install runtime dependencies
	$(UV) sync

dev:  ## Install everything, including dev and extras
	$(UV) sync --all-extras --group dev

test:  ## Run the test suite (offline: no token, no network)
	$(UV) run pytest

test-cov:  ## Run tests with a coverage report
	$(UV) run pytest --cov=tinvest_mcp --cov-report=term-missing

lint:  ## Lint
	$(UV) run ruff check .

fmt:  ## Format
	$(UV) run ruff format .

check: lint test  ## Lint and test — run before opening a pull request

doctor:  ## Print the resolved configuration and startup security checks
	$(UV) run tinvest-mcp-doctor

run:  ## Start the MCP server on the configured transport (stdio by default)
	$(UV) run tinvest-mcp

smoke:  ## End-to-end sandbox loop with virtual money (needs TINVEST_SANDBOX_TOKEN)
	$(UV) run python -m tinvest_mcp.scripts.sandbox_smoke

debug-api:  ## Local Swagger UI on 127.0.0.1:8099 — development only, never expose
	$(UV) run python -m tinvest_mcp.debug_api

docker:  ## Build and start the container (HTTP transport, loopback only)
	docker compose up --build

clean:  ## Remove caches and build artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
