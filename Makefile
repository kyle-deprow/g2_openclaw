# ============================================================================
# G2 OpenClaw — Unified Makefile
# ============================================================================
# Usage: make <target>
#   Run `make help` to see all available targets.
# ============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Colors
CYAN  := \033[36m
GREEN := \033[32m
BOLD  := \033[1m
RESET := \033[0m

# MemPalace paths
MEMPALACE_VENV    := $(HOME)/.local/share/mempalace/venv
MEMPALACE_PYTHON  := $(MEMPALACE_VENV)/bin/python

# ============================================================================
# Setup
# ============================================================================

.PHONY: cold-start setup

cold-start: ## Full cold start: deps, env, security checks, smoke tests
	@bash scripts/bootstrap.sh

setup: cold-start ## Alias for cold-start

# ============================================================================
# Testing
# ============================================================================

.PHONY: test test-gateway test-integration test-g2

test: test-gateway test-integration test-g2 ## Run all tests across all components

test-gateway: ## Run gateway unit tests
	@echo -e "$(CYAN)$(BOLD)>>> Gateway tests$(RESET)"
	@uv run pytest tests/gateway/ -v

test-integration: ## Run integration tests
	@echo -e "$(CYAN)$(BOLD)>>> Integration tests$(RESET)"
	@uv run pytest tests/integration/ -v

test-g2: ## Run G2 App tests
	@echo -e "$(CYAN)$(BOLD)>>> G2 App tests$(RESET)"
	@cd g2_app && npm test

# ============================================================================
# Linting & Formatting
# ============================================================================

.PHONY: lint lint-python lint-g2 format format-python typecheck typecheck-python

lint: lint-python lint-g2 ## Lint all components

lint-python: ## Lint Python with ruff
	@echo -e "$(CYAN)$(BOLD)>>> Ruff check$(RESET)"
	@uv run ruff check .

lint-g2: ## Type-check G2 App
	@echo -e "$(CYAN)$(BOLD)>>> G2 App typecheck$(RESET)"
	@cd g2_app && npx tsc --noEmit

format: format-python ## Format all components

format-python: ## Format Python with ruff
	@echo -e "$(CYAN)$(BOLD)>>> Ruff format$(RESET)"
	@uv run ruff format .

typecheck: typecheck-python lint-g2 ## Run all type checks (mypy + tsc)

typecheck-python: ## Type-check Python with mypy
	@echo -e "$(CYAN)$(BOLD)>>> mypy$(RESET)"
	@uv run mypy gateway/ infra/

# ============================================================================
# Pre-commit
# ============================================================================

.PHONY: pre-commit

pre-commit: ## Run all pre-commit hooks
	@uv run pre-commit run --all-files

# ============================================================================
# Gateway Operations
# ============================================================================

.PHONY: init-env launch stop sim restart push-config mempalace-install

init-env: ## Generate .env from system detection
	@uv run python -m gateway init-env

launch: ## Start the gateway server (foreground, Ctrl+C to stop)
	@uv run python -m gateway launch

stop: ## Stop all G2 OpenClaw processes
	@uv run python -m gateway stop

sim: ## Stop all services and re-launch the full sim stack
	@uv run python -m gateway stop
	@uv run python -m gateway launch --daemon

restart: sim ## Alias for sim

push-config: ## Push OpenClaw config to the gateway
	@uv run python -m gateway push-config

mempalace-install: ## Install MemPalace MCP server (idempotent)
	@echo -e "$(CYAN)$(BOLD)>>> Installing MemPalace...$(RESET)"
	@if [ -d "$(MEMPALACE_VENV)" ]; then \
		echo "MemPalace venv exists, upgrading..."; \
		uv pip install --python $(MEMPALACE_PYTHON) --upgrade mempalace 2>&1 | tail -1; \
	else \
		uv venv $(MEMPALACE_VENV); \
		uv pip install --python $(MEMPALACE_PYTHON) mempalace 2>&1 | tail -1; \
	fi
	@echo -e "$(GREEN)$(BOLD)>>> MemPalace ready$(RESET)"


# ============================================================================
# G2 App Deploy
# ============================================================================

.PHONY: deploy deploy-dev

deploy: ## Build, package, and serve G2 app for phone sideloading
	@echo -e "$(CYAN)$(BOLD)>>> Building G2 app for production...$(RESET)"
	@cd g2_app && npm run pack
	@echo -e "$(GREEN)$(BOLD)>>> Package ready: g2_app/g2-openclaw.ehpk$(RESET)"
	@echo ""
	@echo -e "$(BOLD)Next steps:$(RESET)"
	@echo "  1. Serve the package:  cd g2_app && npx serve dist -l 3000"
	@echo "  2. Generate QR code:   cd g2_app && evenhub qr --url http://$$(hostname -I | awk '{print $$1}'):3000"
	@echo "  3. Scan QR with EvenHub iOS app"
	@echo ""
	@echo -e "$(BOLD)Or use 'make deploy-dev' to deploy via Vite dev server (hot reload).$(RESET)"

deploy-dev: ## Serve G2 app via Vite + generate QR for phone sideloading
	@echo -e "$(CYAN)$(BOLD)>>> Starting G2 app dev server on 0.0.0.0:5173...$(RESET)"
	@echo -e "$(BOLD)Scan the QR code below with EvenHub iOS app:$(RESET)"
	@cd g2_app && npx evenhub qr --http --port 5173 --ip $$(hostname -I | awk '{print $$1}')
	@echo ""
	@echo "Starting Vite dev server (Ctrl+C to stop)..."
	@cd g2_app && npm run dev:network

# ============================================================================
# Infrastructure
# ============================================================================

.PHONY: infra-validate infra-deploy infra-destroy infra-lint

infra-validate: ## Validate Bicep templates
	@uv run azure-infra-cli validate

infra-deploy: ## Deploy Azure infrastructure
	@uv run azure-infra-cli deploy

infra-destroy: ## Destroy Azure infrastructure
	@uv run azure-infra-cli destroy

infra-lint: ## Lint Bicep templates
	@uv run azure-infra-cli lint

# ============================================================================
# Cleanup
# ============================================================================

.PHONY: clean

clean: ## Remove caches, dist/, logs/, node_modules/
	@echo -e "$(CYAN)$(BOLD)>>> Cleaning up...$(RESET)"
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	@rm -rf dist/ logs/
	@rm -rf g2_app/node_modules g2_app/dist
	@echo -e "$(GREEN)$(BOLD)>>> Clean complete$(RESET)"

# ============================================================================
# Help
# ============================================================================

.PHONY: help

help: ## Show all targets with descriptions
	@echo -e "$(BOLD)G2 OpenClaw — Available targets:$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""
