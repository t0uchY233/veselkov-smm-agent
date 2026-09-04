ALTER TABLE releases ADD COLUMN revision_target TEXT;

CREATE TABLE artifact_versions (
    artifact_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    kind TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size INTEGER NOT NULL CHECK (size >= 0),
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    UNIQUE (release_id, kind, version)
);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    gate TEXT NOT NULL CHECK (gate IN ('plan', 'editorial', 'final')),
    decision TEXT NOT NULL CHECK (decision IN ('pending', 'approved', 'rejected', 'invalidated')),
    release_revision INTEGER NOT NULL,
    artifact_set_hash TEXT NOT NULL CHECK (length(artifact_set_hash) = 64),
    actor TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE UNIQUE INDEX ux_approvals_one_pending
ON approvals(release_id) WHERE decision = 'pending';

CREATE TABLE sources (
    source_id TEXT NOT NULL,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    evidence_excerpt_hash TEXT NOT NULL CHECK (length(evidence_excerpt_hash) = 64),
    PRIMARY KEY (release_id, source_id)
);

CREATE TABLE claims (
    claim_id TEXT NOT NULL,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    exact_text TEXT NOT NULL,
    materiality TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (release_id, claim_id)
);

CREATE TABLE claim_sources (
    release_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports',
    PRIMARY KEY (release_id, claim_id, source_id),
    FOREIGN KEY (release_id, claim_id) REFERENCES claims(release_id, claim_id),
    FOREIGN KEY (release_id, source_id) REFERENCES sources(release_id, source_id)
);

CREATE TABLE visual_specs (
    visual_id TEXT NOT NULL,
    release_id TEXT NOT NULL REFERENCES releases(release_id),
    position INTEGER NOT NULL CHECK (position BETWEEN 1 AND 5),
    kind TEXT NOT NULL,
    anchor_text TEXT NOT NULL,
    purpose TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifact_versions(artifact_id),
    claim_ids_json TEXT NOT NULL CHECK (json_valid(claim_ids_json)),
    PRIMARY KEY (release_id, visual_id),
    UNIQUE (release_id, position),
    UNIQUE (release_id, anchor_text)
);
