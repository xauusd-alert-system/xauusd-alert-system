# Rolling Hold-Out Policy (`HOLDOUT_ROLL_POLICY.md`)

**Status:** preregistered · **Owner action required for lock shifts** · **Date:** 2026-08-30

## 1. Why this policy exists

`scripts/overnight.py` post-fix keeps the nightly retrain frozen on data up to
`validation.locked_holdout.start` (currently `2026-08-08`). The safety freeze
(`retraining.enabled: false`, `config.yaml`) is also active. As a result the
production model **silently ages** on the live-forward accumulation after the
lock: research may never look at that data, yet the model is trained only on the
pre-lock window.

A frozen model is safe but decays. This policy makes the **lock shift** a
conscious, preregistered, journalled act instead of a silent config edit — so
we can give fresh data back to production on a known, auditable cadence without
ever peeking at the live-forward window during research.

> **Rule (standing):** shifting the lock is the **owner's act, performed in a
> real terminal**. It is **never** executed from the sandbox/automation. The
> scripts in this policy only *propose*, *record*, and *verify*.

## 2. Preregistered constants (no fitting to outcomes)

These live in `config.yaml` under `holdout_roll:` (copied verbatim so they are
reviewable without reading code):

| Constant | Value | Meaning |
|---|---|---|
| `baseline_start` | `"2026-08-08"` | Lock start this policy was bootstrapped from; the CI guard's acceptable config value when the journal is empty. |
| `cadence_days` | `30` | How often a lock shift *may* be proposed (~monthly). |
| `step_days` | `14` | How far the lock advances each shift (`+14d`), leaving a 14-day overlap so each new candidate is still validated partly on already-released data. |
| `primary_metric` | `expectancy` | Gate comparison metric (via `deploy_guard.is_improvement`). |
| `fallback_metrics` | `[sharpe_ratio, win_rate, total_pnl]` | Fallback chain when the primary is degenerate. |
| `tolerance` | `0.0` | Allowed degradation of candidate vs incumbent on the released slice. `0.0` = candidate must be at least as good (strictly conservative). |
| `released_min_trades` | `20` | Minimum OOS trades on `[old, new)` for the comparison to be trusted. A thin side never drives a shift. |

These are changed **only** by an explicit, reviewed config edit — never by
editing the gate code to chase a green result.

## 3. The gate (`scripts/holdout_roll.py::propose`)

For a candidate shift `old_start → new_start`:

1. **Candidate training (R4, training).** The candidate frame is truncated to
   strictly-before `new_start` *before* feature building. No walk-forward fold is
   ever fit on data at/after the new lock.
2. **Scoring (R4, scoring).** Both the candidate and the incumbent (current
   production model) are scored **only** on the released slice
   `[old_start, new_start)`. Only walk-forward test windows fully inside that
   slice are used (`released_windows`).
3. **Decision.** `deploy_guard.is_improvement(incumbent, candidate, …)` decides
   whether the candidate may replace the incumbent, using the preregistered
   `primary_metric` / `fallback_metrics` / `tolerance` / `released_min_trades`.
4. **Record.** A `PROPOSED` row is appended to `logs/holdout_roll_journal.csv`
   (append-only) with the full gate metrics.

The gate passes only if **every evaluated asset** passes `is_improvement`.

`propose` does **not** edit config and does **not** move the lock.

## 4. Human-in-the-loop workflow

```
propose  ──▶  owner reviews gate_metrics  ──▶  move  ──▶  owner edits config + commits
   │                                                      ▲
   │                                                      │ (real terminal only)
   └──────────────────────────────────────────────────────┘
```

1. **Propose** (anyone, sandbox OK):
   ```bash
   python scripts/holdout_roll.py propose \
       --old-start 2026-08-08 --new-start 2026-08-22 [--asset XAUUSD]
   ```
   Reads the gate verdict. The lock is **not** moved.
2. **Owner OK.** The owner reviews `gate_metrics_json` (candidate vs incumbent on
   the released slice). A green gate is necessary but not sufficient — the owner
   confirms the step is on-cadence and the released window is representative.
3. **Move** (records intent; still no config edit):
   ```bash
   python scripts/holdout_roll.py move \
       --old-start 2026-08-08 --new-start 2026-08-22
   ```
4. **Owner shifts the lock in a real terminal** (see §6). The journal row is the
   audit trail and MUST be committed together with the config shift, or the CI
   guard (§7) will not see it after checkout and will falsely fail. **`logs/` is
   gitignored (`logs/*.csv`), so the journal must be force-added:**
   ```bash
   # edit config.yaml: validation.locked_holdout.start: "2026-08-22"
   git add config/config.yaml
   git add -f logs/holdout_roll_journal.csv   # audit trail, tracked despite logs/*.csv
   git commit -m "holdout: roll lock 2026-08-08 -> 2026-08-22"
   ```
   CI (§7) then verifies config == journal.

## 5. Rollback runbook

If metrics degrade after a shift (or the owner changes their mind):

1. **Record the rollback** (no config edit):
   ```bash
   python scripts/holdout_roll.py rollback --new-start 2026-08-22
   # or, if the predecessor cannot be auto-resolved:
   python scripts/holdout_roll.py rollback --new-start 2026-08-22 --revert-to 2026-08-08
   ```
2. **Owner reverts the lock in a real terminal** and commits the journal together
   with the config (force-add, same reason as §4 step 4 — `logs/*.csv` is
   gitignored):
   ```bash
   # edit config.yaml: validation.locked_holdout.start: "2026-08-08"
   git add config/config.yaml
   git add -f logs/holdout_roll_journal.csv
   git commit -m "holdout: roll back lock to 2026-08-08"
   ```
3. The journal row carries `decision=ROLLED_BACK` and `rollback_of=<the start
   undone>`, so the CI guard's effective current start reverts correctly. The
   committed journal is what CI reads, so the force-add above is mandatory.

**Reverting a config commit** (if the shift was already committed to git): the
owner resets `validation.locked_holdout.start` to the predecessor in a new commit
(`git revert <sha>` or a targeted amend on the branch) — this is a normal config
change, not a model change.

## 6. Operational section — R1 (retraining freeze states)

The lock shift and the safety freeze are **independent**:

- **`retraining.enabled: false` (current state).** Moving the lock alone changes
  **only** the research cutoff. The nightly retrain stays off, so no model is
  retrained automatically. After a lock shift the owner must separately decide
  whether to re-enable `retraining.enabled` (and run the pre-lock A/B + MTF
  revalidation first). A lock shift does **not** imply retraining.
- **`retraining.enabled: true`.** Moving the lock lets the next nightly retrain
  consume the freshly released `[old_start, new_start)` window. The incumbent is
  still protected by `deploy_guard --check` (which now also respects the lock —
  see R2 below), so a regressed nightly model is rolled back automatically.

Either way, the lock-shift workflow in §4 is identical; only the downstream
effect on the nightly differs. The runbook must state which state is active when
the shift is applied.

## 7. CI guard (`scripts/check_holdout_roll.py`)

A blocking CI job (`holdout-roll-guard` in `.github/workflows/ci.yml`) enforces:

> `config.validation.locked_holdout.start` MUST equal the journal's effective
> current start.

Effective current start is derived from `logs/holdout_roll_journal.csv`:

| Journal state | Expected config `locked_holdout.start` |
|---|---|
| empty | `holdout_roll.baseline_start` |
| last row `MOVED` | last `new_start` |
| last row `ROLLED_BACK` | last `new_start` (the value config reverted to) |
| last row `PROPOSED` | last `old_start` (move not yet applied; config unchanged) |

Drift is also caught: a config already at the proposed `new_start` with **no**
`MOVED` row is an out-of-band edit (config changed but not journalled).

Run locally:
```bash
python scripts/check_holdout_roll.py   # exit 0 = consistent, 1 = drift
```

## 8. Journal format (`logs/holdout_roll_journal.csv`, append-only)

Columns: `ts, old_start, new_start, gate_metrics_json, decision, actor, rollback_of`

- `decision` ∈ `PROPOSED | MOVED | ROLLED_BACK`
- `gate_metrics_json` — for `PROPOSED`, the full `propose` result (per-asset
  `is_improvement` verdicts, released fold counts, gate_pass). Empty for `MOVE` /
  `ROLLBACK`.
- `actor` — who ran the command (proposer / owner).
- `rollback_of` — for `ROLLED_BACK`, the `new_start` being undone; empty otherwise.

This file is the system of record for *what the lock should be*. Config on disk
must always agree with it (verified by §7).

## 9. R2 fix (deploy guard no longer scores post-lock data)

`deploy_guard --check` previously validated the freshly retrained candidate
against the incumbent on the **full** backfilled history, which included the
locked hold-out window — i.e. it scored live-forward data research is forbidden
to look at. `guard_asset` now truncates the raw frame to strictly-before
`locked_holdout_end_date(cfg)` before feature building, and `--check` gains an
`--end-date` override for ad-hoc validation ranges. The incumbent is a static
file; the candidate is trained per fold; neither side is scored at/after the
lock. This keeps the deploy guard honest and consistent with the training cutoff.
