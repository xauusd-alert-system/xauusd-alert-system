.PHONY: test test-cov lint format run-bot run-health paper clean

test:
	pytest tests/ -v

test-cov:
	pytest tests/ --cov=usstocks --cov=shared --cov-report=term-missing --cov-fail-under=90

lint:
	ruff check .

format:
	ruff format .

run-bot:
	PROFILE=us_stocks_challenge python -m usstocks.bot

run-health:
	PROFILE=us_stocks_challenge python -m usstocks.health_server

paper:
	PROFILE=replay python -m usstocks.paper --csv-root data/replay --dates 2026-08-27

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache .coverage
