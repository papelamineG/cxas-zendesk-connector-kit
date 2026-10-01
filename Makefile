# CXAS <-> Zendesk connector kit
#   make install-dev   # runtime + test dependencies + Chromium for Playwright
#   make run           # http://localhost:8080 (sandbox mode unless ZENDESK_API_TOKEN is set)
#   make test          # all tests (unit + MCP + Playwright E2E)

PYTHON ?= python3
PORT ?= 8080
IMAGE ?= cxas-zendesk-connector
PROJECT_ID ?=
LOCATION ?= us
APP_ID ?=
BACKEND_URL ?=

.PHONY: help install install-dev run run-mock test test-unit test-e2e docker-build docker-run setup-cxas setup-cxas-dry-run clean

help:
	@grep -E '^[a-zA-Z_-]+:.*' Makefile | grep -v '^\.' | cut -d: -f1 | sort | sed 's/^/  make /'

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev: install
	$(PYTHON) -m pip install "pytest>=8" "playwright>=1.45"
	$(PYTHON) -m playwright install chromium

run:
	$(PYTHON) -m uvicorn backend.server:app --host 0.0.0.0 --port $(PORT) --reload

run-mock:
	ZENDESK_MOCK_MODE=true $(PYTHON) -m uvicorn backend.server:app --host 0.0.0.0 --port $(PORT) --reload

test:
	$(PYTHON) -m pytest tests/ -q

test-unit:
	$(PYTHON) -m pytest tests/test_escalation_relay.py tests/test_zendesk_mcp_tools.py -q

test-e2e:
	$(PYTHON) -m pytest tests/test_e2e_browser.py -q

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p $(PORT):8080 --env-file $(or $(wildcard .env),/dev/null) $(IMAGE)

setup-cxas:
	$(PYTHON) scripts/setup_cxas.py --project-id $(PROJECT_ID) --location $(LOCATION) --app-id $(APP_ID) --backend-url $(BACKEND_URL)

setup-cxas-dry-run:
	$(PYTHON) scripts/setup_cxas.py --project-id $(or $(PROJECT_ID),my-project) --location $(LOCATION) --app-id $(or $(APP_ID),my-app) --backend-url $(or $(BACKEND_URL),https://example.run.app) --dry-run

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
