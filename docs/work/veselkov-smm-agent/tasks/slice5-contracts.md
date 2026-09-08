# Slice 5 contracts: recovery, incidents and notification

## Delivered boundary

- `0005_recovery.sql` adds fenced execution metadata to durable jobs and their
  attempts, keeping Slice 4 rows in place.  `lease_epoch = 0` and nullable
  identity/fence values explicitly mean legacy unknown, never inferred state.
- Recovery payloads persist platform, payload hash, publication idempotency
  key and operation key for status-first reconciliation.
- Incidents are local durable records.  Notifications and their receipts are
  separate technical objects and are constrained to Sardor's Telegram ID
  `276042853`.
- Retry classification is pure and provider-independent: timeout, HTTP 429,
  HTTP 5xx and unknown outcome retry at 30s, 2m, 5m, 15m for provider work;
  notification delivery retries at 1m, 5m, 15m.  Auth, permission, invalid
  payload and receipt mismatch are terminal.

## Verification

- Generated all recovery-related JSON schemas with
  `PYTHONPATH=src .venv/bin/python tools/generate_contracts.py`.
- Guarded contract and migration checks passed: `5 passed`.
- Guarded Ruff check passed.
- Guarded strict Mypy check for recovery contracts/domain service passed.

## Self-review disposition

No critical or high contract findings remain.  The Slice 5 job engine owns
atomic claim/heartbeat/fence enforcement and will populate the new identity
columns for new work.  This is deliberately deferred from this contract slice;
backfilling a legacy provider identity is unsafe and prohibited by the
migration's explicit unknown state.

## Downstream seam

`slice5/job-engine` consumes `RecoveryJobPayload`, `RecoveryError`,
`RetryDecision` and the new `jobs`/`job_attempts` columns.  `slice5/reconciliation`
uses the persisted publication identity fields for status-first retry, while
`slice5/notification` persists `IncidentRecord`, `NotificationRequest`,
`NotificationJobPayload` and `NotificationReceipt` without an outward send in
this slice.
