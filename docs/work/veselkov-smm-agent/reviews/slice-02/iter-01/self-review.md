# Builder review findings

Date: 2026-09-04

This is a separate builder review pass, not an independent approval.

| Severity | Location before fix | Finding | Disposition |
|---|---|---|---|
| High | `platform/db.py`, migrations | A migration could be recorded or applied partially | Migration runner now wraps each unapplied script and version record in one SQLite transaction; upgrade test added |
| High | editorial revision flow | Invalidated package files remained in status and could be reused | Artifact versions now carry `valid`; target-specific invalidation hides stale outputs and is integration-tested |
| High | editorial provenance | Manifest could claim arbitrary TOV/Humanize versions | CLI reads the TOV hash and locked Humanize hash; import rejects a mismatch |
| High | editorial assets | File existence was checked, but dimensions and decodability were not | Pillow adapter verifies 1280x720 cover and 1080x1080 visuals before mutation |
| Medium | editorial deliverables | Required DOCX preview was absent | DOCX adapter now places the cover, text and each visual at its anchor |
| Medium | command audit | Editorial command results stored a generic command name | Each command now stores its exact operation name |
| Medium | repeated editorial import | `INSERT OR REPLACE` could violate foreign keys or leave stale rows | Current editorial records are cleared transactionally and upserts no longer delete referenced parents |
| Medium | CLI failures | JSON errors were printed with process exit code zero | Domain failures now exit 2; malformed commands also return one JSON error document |

After fixes, the full gate was rerun. Final product risk acceptance remains with
Sardor under `AGENTS.md`.
