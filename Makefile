.PHONY: install dev-install run test test-cov migrate migrate-new typecheck clean

install:
	pip install -e . --break-system-packages

dev-install:
	pip install -e ".[dev]" --break-system-packages

run:
	python3 -m mnemosyne.main

test:
	pytest -v

test-cov:
	pytest --cov=mnemosyne --cov-report=term-missing

migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate -m "$(msg)"

typecheck:
	mypy src

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache htmlcov .coverage
