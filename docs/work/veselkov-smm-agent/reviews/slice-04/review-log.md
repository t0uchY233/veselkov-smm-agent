# Slice 4 independent review log

Reviewer: `gpt-5.6-terra`, reasoning `xhigh`, fresh context on every pass.
The reviewer was read-only and inspected the complete repository artifact against
`spec.md` and `plan.md`.

## Pass 1

The review found unsafe cancellation, use of a Dzen draft link, missing remote
receipts/operation ownership, unchecked provider targets, an insufficient target
window, an unvalidated job payload, and unavailable rescheduling. The implementation
was changed to confirm cancellation before `delayed`, arm Dzen before building the
Telegram caption, persist receipts behind a lease, validate every provider response,
require a 35-minute lead, validate versioned job payloads, and support rescheduling.

## Pass 2

The review found that the executable worker did not call publication coordination,
stale targets could pass the final gate, overdue preflight could overwrite a public
receipt, restarts did not resume stored receipts, and due jobs lacked an atomic
claim. These paths received executable replay wiring, final-gate target validation,
monotonic public receipts, receipt-based resume, and claimed job attempts.

## Pass 3

The review found stale successful preflight replay, non-durable executable replay
state, weak URL host validation, and target-time Telegram work that was only queued.
The implementation now uses an attempt-specific preflight command, persistent replay
state, exact YouTube hosts, immutable Telegram job inputs, and a target-time handler.

## Pass 4

The review found a crash window after the Telegram send, no native-platform
reconciliation at target, and lost cancelled-attempt history. The handler now accepts
an already-public idempotent receipt, commits the receipt and claimed job together,
reconciles all three replay platforms to `published`, emits canonical publication
events, and retains cancelled attempts.

## Deliberate slice boundaries

- Real YouTube, Dzen, Telegram Bot API, Windows Task Scheduler, wake behavior, and
  the provider-backed CLI reschedule wiring remain Slice 6 capability work.
- Retry exhaustion, cancellation-job consumption, heartbeats, and partial-public
  recovery remain Slice 5. Slice 4 preserves receipts and enters `recovering` or
  `needs_attention` without deleting public content.
