.PHONY: help dev test cov lint fmt security demo mcp clean ci
PY ?= python3

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "\033[36m%-10s\033[0m %s\n", $$1, $$2}'
dev:      ## Install with dev extras
	$(PY) -m pip install -e ".[dev]"
test:     ## Run the test suite
	$(PY) -m pytest -q
cov:      ## Test with coverage
	$(PY) -m pytest --cov=src/career_agent --cov-report=term-missing
lint:     ## Lint
	$(PY) -m ruff check src tests && $(PY) -m ruff format --check src tests
fmt:      ## Auto-format
	$(PY) -m ruff format src tests && $(PY) -m ruff check --fix src tests
security: ## SAST
	$(PY) -m bandit -q -r src/career_agent -ll
demo:     ## Analyse both example postings
	$(PY) -m career_agent.cli analyse examples/jpmorgan_hyderabad.yaml
	$(PY) -m career_agent.cli analyse examples/senior_ml_riyadh.yaml
mcp:      ## Run the MCP server on stdio
	$(PY) -m career_agent.mcp_server
ci: lint security test  ## Everything CI runs
clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage data resumes
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
