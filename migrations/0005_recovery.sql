-- Slice 5 recovery state.  ALTER TABLE preserves Slice 4 rows in place: a
-- NULL identity/fence means that a historical record was created before
-- fenced recovery existed, rather than an identity guessed during migration.

ALTER TABLE jobs ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0
    CHECK (lease_epoch >= 0);
ALTER TABLE jobs ADD COLUMN active_attempt_id TEXT;
ALTER TABLE jobs ADD COLUMN last_heartbeat_at TEXT;
ALTER TABLE jobs ADD COLUMN last_error_code TEXT;
ALTER TABLE jobs ADD COLUMN last_error_detail TEXT;
ALTER TABLE jobs ADD COLUMN last_error_at TEXT;

ALTER TABLE job_attempts ADD COLUMN worker_id TEXT;
ALTER TABLE job_attempts ADD COLUMN fence_token TEXT;
ALTER TABLE job_attempts ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0
    CHECK (lease_epoch >= 0);
ALTER TABLE job_attempts ADD COLUMN last_heartbeat_at TEXT;

-- A provider request and its recovery operation have distinct durable
-- identities.  They are intentionally nullable for existing Slice 4 rows;
-- recovery code must reconcile their remote receipt before any side effect.
ALTER TABLE publications ADD COLUMN idempotency_key TEXT;
ALTER TABLE publications ADD COLUMN operation_key TEXT;
ALTER TABLE publication_attempts ADD COLUMN idempotency_key TEXT;
ALTER TABLE publication_attempts ADD COLUMN operation_key TEXT;

CREATE UNIQUE INDEX ux_publications_idempotency_key
ON publications(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX ux_publication_attempts_idempotency_key
ON publication_attempts(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX ix_jobs_recovery_claim
ON jobs(state, due_at, lease_until, lease_epoch);

CREATE UNIQUE INDEX ux_job_attempts_fence
ON job_attempts(job_id, lease_epoch)
WHERE lease_epoch > 0;

CREATE TABLE incidents (
    incident_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    job_id TEXT REFERENCES jobs(job_id),
    platform TEXT CHECK (platform IN ('youtube', 'dzen', 'telegram')),
    error_code TEXT NOT NULL,
    sanitized_detail TEXT,
    safe_next_action TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'acknowledged', 'resolved')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX ix_incidents_release_open
ON incidents(release_id, state, created_at);

CREATE TABLE notifications (
    notification_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    recipient TEXT NOT NULL DEFAULT '276042853'
        CHECK (recipient = '276042853'),
    channel TEXT NOT NULL CHECK (channel = 'telegram_alert'),
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'sending', 'delivered', 'retry_wait', 'failed'
    )),
    suppression_key TEXT NOT NULL UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    delivered_at TEXT,
    last_error_code TEXT,
    last_error_detail TEXT,
    last_error_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_notifications_due_state
ON notifications(state, updated_at);

CREATE TABLE notification_attempts (
    attempt_id TEXT PRIMARY KEY,
    notification_id TEXT NOT NULL REFERENCES notifications(notification_id),
    job_attempt_id TEXT REFERENCES job_attempts(attempt_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT CHECK (outcome IN ('delivered', 'retry_wait', 'failed', 'cancelled')),
    provider_receipt_id TEXT,
    error_code TEXT,
    sanitized_detail TEXT
);

CREATE INDEX ix_notification_attempts_notification
ON notification_attempts(notification_id, started_at);
