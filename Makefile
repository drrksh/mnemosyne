.PHONY: install dev-install run serve test test-postgres test-cov lint typecheck check eval bench docker migrate migrate-new clean

PYTHON ?= python3
PG_URL ?= postgresql+psycopg://mnemosyne:mnemosyne@127.0.0.1:5432/mnemosyne_test

install:
	$(PYTHON) -m pip install -e .

dev-install:
	$(PYTHON) -m pip install -e ".[dev,postgres]"

run serve:
	mnemosyne serve

test:
	$(PYTHON) -m pytest -q

test-postgres:
	MNEMOSYNE_TEST_POSTGRES_URL=$(PG_URL) $(PYTHON) -m pytest -q tests/integration/test_postgres.py

test-cov:
	$(PYTHON) -m pytest -q --cov=mnemosyne --cov-report=term-missing:skip-covered

lint:
	ruff check src tests benchmarks

typecheck:
	mypy src

check: lint typecheck test

eval:
	mnemosyne eval
	mnemosyne eval --heldout
	mnemosyne eval --context 1500
	mnemosyne eval --context 1500 --heldout

bench:
	$(PYTHON) benchmarks/scale.py 10000
	$(PYTHON) benchmarks/hook_latency.py --runs 20

docker:
	docker build -t mnemosyne:latest .

migrate:
	mnemosyne db upgrade

migrate-new:
	alembic revision --autogenerate -m "$(msg)"

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist *.egg-info
