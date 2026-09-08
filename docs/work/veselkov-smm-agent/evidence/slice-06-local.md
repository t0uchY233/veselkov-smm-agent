# Slice 6 local verification evidence

Date: 2026-09-04
Status: local foundation verified; Windows/live capability gate pending

## Observable result

- A versioned TOML configuration requires explicit Windows paths, task account,
  credential references, target channels and Dzen author identity. No secret value
  is accepted in the configuration.
- `smm-worker --config ... --once` is the production composition seam. It fails
  closed until the non-production live smoke is accepted; replay mode remains
  explicit and cannot be selected implicitly.
- YouTube has a durable resumable-upload receipt, status-query recovery for
  ambiguous 308/timeouts, bounded processing checks, post-processing thumbnail,
  native `publishAt` and explicit read-back.
- Telegram stores task state in SQLite with CAS/fencing, streams multipart video,
  keeps the bot token inside the wire transport, and never automatically repeats
  an ambiguous send.
- Dzen binds the persistent browser session and durable receipt to the configured
  channel, author, article ID/URL, title and approved content hashes. Its optional
  Playwright session is visible/headful and does not automate login, MFA or CAPTCHA.
- Windows adapters generate and register exact T-30/T Task Scheduler plans, check
  task-account consistency and expose NTFS ACL checks. Wake capability is not
  inferred from XML generation or host detection.
- `smmctl capability smoke` emits a redacted machine-readable non-production
  report and remains `productionReadiness=blocked` even when injected fake probes
  pass.

## Reproducible local checks

All test commands ran sequentially through the mandated guard.

```text
/root/.local/bin/codex-test-guard --timeout 20m -- .venv/bin/pytest
146 passed in 379.96s (0:06:19)

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/ruff check .
All checks passed!

/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/mypy --strict src
Success: no issues found in 79 source files
```

Dry-run capability commands also remained fail-closed:

```text
.venv/bin/smmctl capability smoke --config config/smm-agent.example.toml
executionRequested=false; allRequiredSmokesPassed=false; productionReadiness=blocked

.venv/bin/smmctl setup validate --config config/smm-agent.example.toml
schemaValid=true; localFoundationReady=false; productionReadiness=not_assessed
```

The latter result is expected on Linux with placeholder Windows paths.

## Remaining live gate

This evidence does not satisfy Slice 6 acceptance. Completion requires the target
Windows 11 laptop, explicit machine paths/task account, Dzen interactive login,
YouTube OAuth for the intended channel, a Telegram bot plus non-production channel,
and permission to register a task and perform a sleep/wake test. The resulting
sanitized smoke report will become `evidence/slice-06.md`.
