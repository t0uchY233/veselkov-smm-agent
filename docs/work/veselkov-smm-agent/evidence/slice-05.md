# Slice 5 verification evidence

Date: 2026-09-04

## Observable result

- Every durable publication job has a unique attempt, a monotonically increasing
  lease epoch, a 60-second lease and a 20-second heartbeat. A stale owner cannot
  heartbeat, complete, retry or fail work after another worker reclaims it.
- A partial target-time release creates one durable recovery job in the same
  transaction that records `recovering`. Recovery reads remote status first,
  retains every public receipt and executes only a missing Telegram task with its
  original remote and operation identities.
- Provider retries use 30 seconds, 2 minutes, 5 minutes and 15 minutes. Terminal
  or exhausted recovery creates `RecoveryExhausted.v1`, an incident and one alert
  job atomically, then enters `needs_attention`.
- Technical alerts are fixed to Telegram recipient `276042853`, validate their
  entire persisted payload and recover a crash after send through an idempotent
  lookup. Exhausted alert delivery leaves the local incident open.
- Invalid payload, permission/authentication failures, receipt mismatch and Dzen
  DOM mismatch are terminal. Persisted error details pass through a redaction
  boundary.
- A restart after target prioritizes target reconciliation over an overdue T-30
  preflight. Cancellation reconciliation is status-first and never cancels content
  after any public receipt exists.

## Reproducible checks

All commands ran sequentially through the repository-mandated guard.

```text
/root/.local/bin/codex-test-guard --timeout 20m -- .venv/bin/pytest
100 passed in 393.16s (0:06:33)

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/ruff check .
All checks passed!

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/mypy --strict src
Success: no issues found in 63 source files
```

The pre-gate focused compatibility run covered contracts, migrations, job
fencing, notifications, recovery and publication integration: `54 passed`.

## Independent review

One fresh read-only pass used `gpt-5.6-terra` at `xhigh`. It found no critical
defect and three high findings. All three, plus the actionable medium findings,
were fixed before the full gate. Exact disposition is recorded in
`reviews/slice-05/review-log.md`.

## Remaining boundary

Real YouTube, Dzen, Telegram and Windows Task Scheduler transports remain Slice 6.
The replay-backed Slice 5 proof does not claim live provider readiness.
