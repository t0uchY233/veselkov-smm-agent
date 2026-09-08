# Evidence: Slice 1

Date: 2026-09-03
Spec: 0.2.1
Status: implemented; final risk acceptance remains with Sardor

## Demonstrated behavior

- `release start` creates one `topic_received` release in SQLite.
- `release status` in a new process returns the same release and one next action.
- A repeated `command_id` with the same canonical request returns the stored result.
- Reusing it with different input returns `IDEMPOTENCY_CONFLICT`.
- A second active release and nonzero start revision return `STATE_CONFLICT`.
- The initial transition and `ReleaseStarted.v1` event are committed atomically.
- Checked-in JSON Schemas match the canonical Pydantic models.

## Reproducible verification

All commands were run sequentially through the repository-required guard:

```text
/root/.local/bin/codex-test-guard --timeout 10m -- uv run ruff check .
All checks passed!

/root/.local/bin/codex-test-guard --timeout 10m -- uv run mypy
Success: no issues found in 14 source files

/root/.local/bin/codex-test-guard --timeout 10m -- uv run pytest
7 passed in 1.46s
```

## Review pass

A fresh diff review found and corrected:

- transition actor was hardcoded instead of using the command actor;
- `.runtime/` was not excluded from the public repository;
- the initial database omitted the required `ReleaseStarted.v1` audit event;
- the release-state constraint would have blocked later accepted states.

No external service, production credential or public channel was used. This
review is implementation evidence, not self-approval; Sardor retains final risk
acceptance as required by `AGENTS.md`.
