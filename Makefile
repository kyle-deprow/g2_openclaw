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

# ============================================================================
# Setup
# ============================================================================

.PHONY: setup install

setup: ## Run scripts/bootstrap.sh
	@bash scripts/bootstrap.sh

install: ## Install all deps (uv sync + npm install in both TS dirs)
	@echo -e "$(CYAN)$(BOLD)>>> Installing Python dependencies...$(RESET)"
	@uv sync --extra dev
	@echo -e "$(CYAN)$(BOLD)>>> Installing G2 App dependencies...$(RESET)"
	@cd g2_app && npm install
	@echo -e "$(CYAN)$(BOLD)>>> Installing Copilot Bridge dependencies...$(RESET)"
	@cd copilot_bridge && npm install

# ============================================================================
# Testing
# ============================================================================

.PHONY: test test-gateway test-integration test-g2 test-bridge

test: test-gateway test-integration test-g2 test-bridge ## Run all tests across all components

test-gateway: ## Run gateway unit tests
	@echo -e "$(CYAN)$(BOLD)>>> Gateway tests$(RESET)"
	@uv run pytest tests/gateway/ -v

test-integration: ## Run integration tests
	@echo -e "$(CYAN)$(BOLD)>>> Integration tests$(RESET)"
	@uv run pytest tests/integration/ -v

test-g2: ## Run G2 App tests
	@echo -e "$(CYAN)$(BOLD)>>> G2 App tests$(RESET)"
	@cd g2_app && npm test

test-bridge: ## Run Copilot Bridge tests
	@echo -e "$(CYAN)$(BOLD)>>> Copilot Bridge tests$(RESET)"
	@cd copilot_bridge && npm test

# ============================================================================
# Linting & Formatting
# ============================================================================

.PHONY: lint lint-python lint-g2 lint-bridge format format-python format-bridge typecheck typecheck-python

lint: lint-python lint-g2 lint-bridge ## Lint all components

lint-python: ## Lint Python with ruff
	@echo -e "$(CYAN)$(BOLD)>>> Ruff check$(RESET)"
	@uv run ruff check .

lint-g2: ## Type-check G2 App
	@echo -e "$(CYAN)$(BOLD)>>> G2 App typecheck$(RESET)"
	@cd g2_app && npx tsc --noEmit

lint-bridge: ## Lint & type-check Copilot Bridge
	@echo -e "$(CYAN)$(BOLD)>>> Copilot Bridge lint + typecheck$(RESET)"
	@cd copilot_bridge && npm run lint && npm run typecheck

format: format-python format-bridge ## Format all components

format-python: ## Format Python with ruff
	@echo -e "$(CYAN)$(BOLD)>>> Ruff format$(RESET)"
	@uv run ruff format .

format-bridge: ## Format Copilot Bridge with biome
	@echo -e "$(CYAN)$(BOLD)>>> Copilot Bridge format$(RESET)"
	@cd copilot_bridge && npm run format

typecheck: typecheck-python lint-g2 lint-bridge ## Run all type checks (mypy + tsc)

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

.PHONY: init-env launch stop push-config

init-env: ## Generate .env from system detection
	@uv run python -m gateway init-env

launch: ## Start the gateway server
	@uv run python -m gateway launch

stop: ## Stop all G2 OpenClaw processes
	@uv run python -m gateway stop

push-config: ## Push OpenClaw config to the gateway
	@uv run python -m gateway push-config

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
	@rm -rf copilot_bridge/node_modules copilot_bridge/dist
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
