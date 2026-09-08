# Slice 5: fenced durable job engine

## Delivered boundary

- Every new claim receives a fresh immutable `attempt_id`, a monotonic
  `lease_epoch`, worker identity and a default 60-second lease.
- Claiming is atomic for due `queued` / `retry_wait` jobs and abandoned
  `running` jobs. An expired prior attempt is closed as `lease_expired` before
  its replacement is recorded.
- `JobClaim` is the stable mutation authority. Heartbeat, success, retry and
  terminal failure fence on `job_id`, active `attempt_id`, `lease_epoch`,
  `running` state and an unexpired lease. A stale worker gets `False` and
  cannot change the replacement's state.
- Retry accepts the exact `due_at` supplied by the recovery policy and stores
  only `RecoveryError.sanitized_detail`. Terminal failure persists the same
  sanitized audit trail. Cancellation closes an active attempt atomically.
- The old `owner_id` completion call is retained solely as a compatibility
  bridge for the existing Slice 4 worker. Its lookup resolves to the active
  attempt and still uses the full fence in SQL; Slice 5 integrations use
  `JobClaim` directly.

## Verification

- Guarded job-unit suite: `8 passed`.
- Guarded compatibility suite with publication and migration tests: `28 passed`.
- Guarded Ruff and strict Mypy checks for the changed queue boundary passed.

## Self-review disposition

No critical or high finding remains in the queue boundary. Job completion and
heartbeat each use a savepoint, so a missing immutable attempt row cannot
leave a durable job half-finished. Historical Slice 4 `running` rows with a
missing lease are reclaimable, but their nullable identity is not guessed or
backfilled.

## Downstream seam

`slice5/reconciliation` and `slice5/notification` should claim with
`worker_id` and then call `heartbeat`, `mark_retry_wait`, `mark_failed` or
`mark_succeeded` with the returned `JobClaim`. The integration worker can keep
its legacy bridge only until all jobs take the typed seam.
