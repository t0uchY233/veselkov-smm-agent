# Slice 5: recovery reconciliation

## Delivered boundary

- `publication_recovery` consumes a fenced `JobClaim` and a validated
  `RecoveryJobPayload`.
- Before any possible side effect it calls `status` for every persisted
  YouTube, Dzen and Telegram receipt. Native YouTube and Dzen schedules are
  status-only; recovery never prepares or arms them.
- Only a non-public Telegram receipt can be executed, and it uses the durable
  original task identity and operation key. A crash after that remote call is
  recovered by the next status pass without a second execute.
- Confirmed provider state is persisted while keeping payload hash, target,
  remote receipt, idempotency key and `public_at` immutable. Historical nullable
  identities may be filled exactly once after reconciliation.
- Retryable outcomes use the contract's exact durable retry decision. A terminal
  or exhausted attempt enters `needs_attention`, records `RecoveryExhausted.v1`,
  and exposes a structured `RecoveryOutcome` to an integration-owned terminal
  recorder. That recorder is the atomic seam for incident and alert outbox
  persistence; this module does not send alerts itself.
- `Database.complete_release` accepts both `scheduled` and `recovering`, so the
  final recovery transition can atomically close the active release.

## Verification

- Guarded focused suite:
  `.venv/bin/pytest -q tests/integration/test_recovery.py tests/integration/test_publication_pipeline.py`
  — `22 passed`.
- Guarded Ruff on recovery, publication persistence and tests — passed.
- Guarded strict Mypy on recovery and touched platform files — passed.

## Self-review disposition

The status pass completes before Telegram execute and no recovery path calls
`prepare` or `arm`. All local changes occur under the job claim's fenced finish;
losing the lease rolls the transaction back, so a new owner reconciles the same
remote identity. Public receipts are never automatically deleted or rewritten.
