.PHONY: install test lint fmt run evals clean estate estate-status estate-down estate-destroy mocks

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
