PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS releases (
    release_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL CHECK (length(trim(topic)) > 0),
    state TEXT NOT NULL CHECK (state IN (
        'topic_received', 'plan_pending', 'editorial_building', 'editorial_pending',
        'awaiting_recording', 'video_processing', 'final_pending',
        'publication_preparing', 'scheduled', 'publishing', 'published',
        'revision_requested', 'recovering', 'delayed', 'needs_attention'
    )),
    revision INTEGER NOT NULL CHECK (revision > 0),
    target_at_utc TEXT,
    target_timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_releases_single_active
ON releases(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS transitions (
    transition_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS command_results (
    command_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    command TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    outcome_json TEXT NOT NULL CHECK (json_valid(outcome_json)),
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_events (
    event_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
);
