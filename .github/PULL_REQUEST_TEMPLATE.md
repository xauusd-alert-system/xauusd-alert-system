<!-- TZ 6.9: PR checklist -->

## Checklist

- [ ] `pytest -q` passes locally (full suite, no skipped-by-failure)
- [ ] No secrets committed: no `.env`, tokens, keys, `*.sqlite` in the diff
- [ ] DB changes ship as a numbered migration in `data/migrations/` (`python -m scripts.migrate_all --dry-run` passes)
- [ ] New/changed behavior is covered by tests
- [ ] Docs updated if behavior/contract changed (`docs/`, module docstrings)
