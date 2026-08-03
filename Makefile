.PHONY: install test lint fmt run evals clean estate estate-status estate-down estate-destroy mocks up down logs provision

install:
	uv sync --extra dev || pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests
	ruff format --check src tests

fmt:
	ruff check --fix src tests
	ruff format src tests

run:
	uvicorn uione.api.app:app --host 127.0.0.1 --port 8000 --reload

# Golden-task gate. Needs a real model, so it is not part of CI.
evals:
	python scripts/run_evals.py $(ARGS)

# The demo estate: real Gitea and Grafana in Docker, plus mocked vendors for
# the systems nobody can reach without a contract. See docs/ESTATE.md.
estate:
	python scripts/estate.py up

# --- running the whole product in containers ---------------------------------

# The UI, immediately: app + mocked vendors + the real systems, on :8000.
# 8800, not 8000 — see the comment on the port mapping in compose.yaml.
UIONE_HTTP_PORT ?= 8800

up:
	UIONE_HTTP_PORT=$(UIONE_HTTP_PORT) docker compose up -d --build
	@echo ""
	@echo "  UI        http://127.0.0.1:$(UIONE_HTTP_PORT)/"
	@echo "  Gitea     http://127.0.0.1:3300/   Grafana http://127.0.0.1:3400/"
	@echo "  Mattermost http://127.0.0.1:8065/"
	@echo ""
	@echo "  Connect the real systems too:  make provision && docker compose restart app"

down:
	docker compose down

logs:
	docker compose logs -f app

# Creates the accounts and tokens in the running Gitea, Grafana and Mattermost,
# and writes settings the *containerised* app can use — service names rather
# than 127.0.0.1, which inside a container means the container itself.
#
# Runs on the host because creating Gitea's first admin needs a command inside
# its container, and the alternative is giving a provisioning container control
# of the host's Docker daemon.
provision:
	ESTATE_TARGET=compose ESTATE_ENV_FILE=estate/generated.env \
		python scripts/estate.py provision
	@echo ""
	@echo "  Now: docker compose restart app"

estate-status:
	python scripts/estate.py status

estate-down:
	python scripts/estate.py down

estate-destroy:
	python scripts/estate.py destroy

# The mocked half, in the foreground. Loopback only, unauthenticated by design.
mocks:
	python -m uione.vendormocks

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache dist build
