-- Telegram has no server-side schedule or idempotency key.  The local task is
-- therefore the authoritative intent record.  Every state mutation is fenced
-- by ``version`` in SQLiteTelegramTaskStore; a process that lost a race cannot
-- submit a second Bot API send.

CREATE TABLE telegram_tasks (
    remote_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_sha256 TEXT NOT NULL,
    video_path TEXT NOT NULL,
    video_sha256 TEXT NOT NULL,
    video_size INTEGER NOT NULL CHECK (video_size > 0 AND video_size <= 49000000),
    caption TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'prepared', 'armed', 'sending', 'public', 'cancelled'
    )),
    target_at_utc TEXT,
    operation_key TEXT,
    message_id TEXT,
    public_at TEXT,
    -- ``lookup_cursor`` is Bot API's next update offset.  ``send_cursor`` is
    -- snapshotted immediately before persisting the send intent.  A recovery
    -- receipt must be a newer update, plus the channel/message identity below.
    lookup_cursor INTEGER NOT NULL DEFAULT 0 CHECK (lookup_cursor >= 0),
    send_cursor INTEGER CHECK (send_cursor IS NULL OR send_cursor >= 0),
    send_intent_at TEXT,
    confirmed_update_id INTEGER CHECK (confirmed_update_id IS NULL OR confirmed_update_id >= 0),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (state IN ('prepared', 'armed', 'cancelled') AND message_id IS NULL AND public_at IS NULL)
        OR (state = 'sending' AND send_cursor IS NOT NULL AND send_intent_at IS NOT NULL
            AND message_id IS NULL AND public_at IS NULL)
        OR (state = 'public' AND message_id IS NOT NULL AND public_at IS NOT NULL
            AND confirmed_update_id IS NOT NULL)
    )
);

CREATE INDEX ix_telegram_tasks_state_target
ON telegram_tasks(state, target_at_utc);
