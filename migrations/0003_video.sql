ALTER TABLE releases ADD COLUMN recording_window_opened_at TEXT;
ALTER TABLE releases ADD COLUMN recording_watch_initialized_at TEXT;

CREATE TABLE recording_candidates (
    candidate_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    observed_path TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    fingerprint TEXT,
    state TEXT NOT NULL CHECK (state IN (
        'observed', 'stabilizing', 'accepted', 'rejected', 'ambiguous'
    )),
    reason TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE UNIQUE INDEX ux_recording_candidates_open_path
ON recording_candidates(release_id, observed_path)
WHERE state IN ('observed', 'stabilizing');

CREATE TABLE recordings (
    recording_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    candidate_id TEXT NOT NULL UNIQUE REFERENCES recording_candidates(candidate_id),
    source_artifact_id TEXT NOT NULL REFERENCES artifact_versions(artifact_id),
    duration_seconds REAL NOT NULL CHECK (duration_seconds > 0),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    accepted_at TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1))
);

CREATE UNIQUE INDEX ux_recordings_one_valid
ON recordings(release_id) WHERE valid = 1;

CREATE TABLE media_leases (
    release_id TEXT PRIMARY KEY REFERENCES releases(release_id),
    release_revision INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    lease_until TEXT NOT NULL
);

CREATE TABLE approval_artifacts (
    approval_id TEXT NOT NULL REFERENCES approvals(approval_id),
    artifact_id TEXT NOT NULL REFERENCES artifact_versions(artifact_id),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    PRIMARY KEY (approval_id, artifact_id)
);

-- Slice 2 approvals did not persist exact membership. They cannot be safely
-- approved after this migration, so reopen them as an explicit revision
-- instead of guessing which bytes the Author saw.
UPDATE releases
SET state = 'revision_requested',
    revision = revision + 1,
    revision_target = (
        SELECT gate FROM approvals
        WHERE approvals.release_id = releases.release_id
          AND approvals.decision = 'pending'
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE release_id IN (
    SELECT release_id FROM approvals
    WHERE decision = 'pending' AND gate IN ('plan', 'editorial')
);

UPDATE approvals
SET decision = 'invalidated',
    reason = 'migration 0003 requires exact approval membership',
    decided_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE decision = 'pending' AND gate IN ('plan', 'editorial');

CREATE TABLE accepted_media_profiles (
    profile_sha256 TEXT PRIMARY KEY CHECK (length(profile_sha256) = 64),
    profile_json TEXT NOT NULL CHECK (json_valid(profile_json)),
    accepted_by TEXT NOT NULL,
    accepted_at TEXT NOT NULL
);
