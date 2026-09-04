# Slice 4 verification evidence

Date: 2026-09-04

## Observable result

- YouTube and Dzen replay publications are prepared and armed for one UTC target.
- The Dzen scheduled URL exists before the approved Telegram caption is assembled.
- Telegram uses the same target and stores immutable video/caption paths and hashes.
- The worker runs T-30 preflight and target-time execution; replay reaches
  `published` with public receipts for all three platforms.
- A failed preflight confirms cancellation before `delayed`; an unconfirmed cancel
  becomes `needs_attention` and never pretends to be safe.
- Restart and crash-point tests resume persisted receipts without duplicate arm or
  Telegram execute operations.
- Target changes retain cancelled schedule-attempt history and use new remote IDs.

## Reproducible checks

All commands ran sequentially through the repository-mandated guard.

```text
/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/pytest -q tests/integration/test_publication_pipeline.py tests/integration/test_migrations.py tests/unit/test_worker_cli.py tests/integration/test_release_cli.py tests/contract/test_generated_schemas.py
27 passed

/root/.local/bin/codex-test-guard --timeout 20m -- .venv/bin/pytest -q
100%, exit code 0, approximately 338 seconds

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/ruff check src tests tools
All checks passed

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/mypy src
Success: no issues found in 55 source files

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/pytest -q tests/contract/test_generated_schemas.py
1 passed
```

Generated JSON Schemas were refreshed with `.venv/bin/python
tools/generate_contracts.py`; `git diff --check` passed.

## Independent review

Four fresh read-only passes used `gpt-5.6-terra` at `xhigh`. Findings and their
disposition are recorded in `reviews/slice-04/review-log.md`. Critical/high defects
inside the Slice 4 replay boundary were fixed and regression-tested. Provider-backed
Windows/CLI capability and full recovery remain assigned to Slices 5 and 6.
