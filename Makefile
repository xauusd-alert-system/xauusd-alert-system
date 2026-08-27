VENV ?= /home/user/venv
PYTHON = $(VENV)/bin/python
PYTEST = $(VENV)/bin/pytest

.PHONY: test test-cov lint format run-bot run-health paper clean

test:
	$(PYTEST) tests/ -v

test-cov:
	$(PYTEST) tests/ --cov=usstocks --cov=shared --cov-report=term-missing --cov-fail-under=90

lint:
	ruff check .

format:
	ruff format .

run-bot:
	PROFILE=us_stocks_challenge $(PYTHON) -m usstocks.bot

run-health:
	PROFILE=us_stocks_challenge $(PYTHON) -m usstocks.health_server

paper:
	PROFILE=replay $(PYTHON) -m usstocks.paper --csv-root data/replay --dates 2026-08-27

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage
