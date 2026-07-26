.PHONY: install test lint fmt run evals clean

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

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache dist build
