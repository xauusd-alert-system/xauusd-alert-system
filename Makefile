# TZ 6.7: standard entrypoints. Works in WSL/Git-Bash/PowerShell(make).
# Windows note: `make` is not installed by default; these targets are the
# canonical commands — run them directly if make is unavailable.

.PHONY: build test run migrate backup audit

# Build the paper/alerts image (see Dockerfile).
build:
	docker compose build

# Full test suite (tests use mocks; MetaTrader5 not required).
test:
	python -m pytest -q

# Run the paper/alerts stack (bot + dashboard).
run:
	docker compose up

# Apply pending DB migrations (dry-run first, then real run).
migrate:
	python -m scripts.migrate_all --dry-run
	python -m scripts.migrate_all

# Consistent DB + risk-state backup into backups/ with retention.
backup:
	python -m scripts.backup_db

# Repository security audit (secrets, .gitignore, tracked *.sqlite).
audit:
	python -m scripts.security_audit
