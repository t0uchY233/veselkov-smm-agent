# Slice 5 independent review log

Reviewer: `gpt-5.6-terra`, reasoning `xhigh`, fresh read-only context.

## Findings and disposition

1. **High: stale notification owner could commit local state after lease loss.**
   Fixed by fencing notification mutations with the active job attempt and rolling
   back stale changes. The transport contract now requires idempotency by
   `notification_id`; a crash-after-send test proves lookup-before-resend.
2. **High: a failed YouTube or Dzen reconciliation could be attributed to
   Telegram.** Fixed by carrying the actual failed/missing platform into the
   recovery outcome and incident. YouTube and Dzen remain status-only in recovery.
3. **High: invalid payload and Dzen DOM mismatch could enter temporary retry.**
   Fixed with typed provider failures and terminal `DZEN_DOM_MISMATCH`; regression
   tests prove these errors bypass `retry_wait`.
4. **Medium: notification job fields were parsed but not bound to the stored
   request.** Fixed by validating release, incident, recipient and canonical
   request hash before a side effect.
5. **Medium: cancellation reconciliation had no consumer.** Fixed with a
   status-first handler. If a public receipt exists it performs no cancel action;
   unresolved cancellation remains visible for human attention.
6. **Medium: redaction depended on caller discipline.** Fixed with a shared
   persistence-boundary sanitizer used by jobs and incidents.

## Additional liveness correction

The root review found that a laptop starting after target could claim the overdue
T-30 job before the target job. The worker now prioritizes target reconciliation
at or after target and closes the obsolete preflight without cancellation.

## Gate

The post-fix full suite, Ruff and strict Mypy all passed. No critical/high finding
remains inside the Slice 5 replay boundary.
