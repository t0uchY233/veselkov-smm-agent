CREATE TABLE publications (
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    platform TEXT NOT NULL CHECK (platform IN ('youtube', 'dzen', 'telegram')),
    state TEXT NOT NULL CHECK (state IN (
        'absent', 'preparing', 'prepared', 'armed', 'publishing', 'public',
        'retry_wait', 'failed', 'cancelled'
    )),
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    remote_id TEXT,
    known_url TEXT,
    target_at_utc TEXT NOT NULL,
    public_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (release_id, platform)
);

CREATE TABLE publication_leases (
    release_id TEXT PRIMARY KEY REFERENCES releases(release_id),
    release_revision INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    lease_until TEXT NOT NULL
);

CREATE TABLE publication_attempts (
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    platform TEXT NOT NULL CHECK (platform IN ('youtube', 'dzen', 'telegram')),
    target_at_utc TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    remote_id TEXT,
    known_url TEXT,
    public_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (release_id, platform, target_at_utc)
);

CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    kind TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled'
    )),
    due_at TEXT NOT NULL,
    lease_until TEXT,
    lease_owner_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    retry_policy_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX ix_jobs_due ON jobs(state, due_at);

CREATE TABLE job_attempts (
    attempt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    error_code TEXT,
    detail TEXT
);
