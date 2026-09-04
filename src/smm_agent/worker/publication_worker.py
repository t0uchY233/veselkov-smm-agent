"""One deterministic polling step for Slice 4 publication coordination."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from smm_agent.application.publication_service import (
    execute_telegram_task,
    preflight_scheduled_release,
    schedule_release,
)
from smm_agent.contracts.publication import Platform, PublicationResult
from smm_agent.domain.publication.ports import Publisher
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.jobs import JobStore


@dataclass(frozen=True, slots=True)
class PublicationTick:
    outcome: str
    publication: PublicationResult | None = None


class PublicationWorker:
    def __init__(
        self, *, database: Database, publishers: dict[Platform, Publisher]
    ) -> None:
        self.database = database
        self.publishers = publishers

    @staticmethod
    def _command_id(
        kind: str, release_id: str, revision: int, attempt_id: str | None = None
    ) -> str:
        material = f"{kind}:{release_id}:{revision}:{attempt_id or 'stable'}"
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"worker-{digest[:32]}"

    def run_once(self, *, now: datetime | None = None) -> PublicationTick:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        instant_text = instant.isoformat().replace("+00:00", "Z")
        job_owner_id = uuid7()
        with self.database.transaction() as connection:
            release = self.database.active_release(connection)
            if release is None:
                return PublicationTick("idle")
            due = (
                JobStore.claim_due(
                    connection,
                    release_id=release.release_id,
                    kind="publication_preflight",
                    now=instant_text,
                    lease_until=(instant + timedelta(seconds=60))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    owner_id=job_owner_id,
                )
                if release.state == "scheduled"
                else None
            )
            telegram_due = (
                JobStore.claim_due(
                    connection,
                    release_id=release.release_id,
                    kind="telegram_publish_reconcile",
                    now=instant_text,
                    lease_until=(instant + timedelta(seconds=60))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    owner_id=job_owner_id,
                )
                if release.state == "scheduled" and due is None
                else None
            )
        if release.state == "publication_preparing":
            result = schedule_release(
                self.database,
                command_id=self._command_id("schedule", release.release_id, release.revision),
                expected_revision=release.revision,
                publishers=self.publishers,
                now=instant,
            )
            outcome = result.state
            return PublicationTick(outcome, result)
        if release.state == "scheduled" and due is not None:
            result = preflight_scheduled_release(
                self.database,
                command_id=self._command_id(
                    "preflight", release.release_id, release.revision, job_owner_id
                ),
                expected_revision=release.revision,
                publishers=self.publishers,
                now=instant,
            )
            if result.state == "scheduled":
                with self.database.transaction() as connection:
                    JobStore.mark_succeeded(
                        connection,
                        job_id=str(due["job_id"]),
                        owner_id=job_owner_id,
                        now=instant_text,
                    )
            outcome = "preflight_ok" if result.state == "scheduled" else result.state
            return PublicationTick(outcome, result)
        if release.state == "scheduled" and telegram_due is not None:
            result = execute_telegram_task(
                self.database,
                command_id=self._command_id(
                    "telegram", release.release_id, release.revision, job_owner_id
                ),
                expected_revision=release.revision,
                publishers=self.publishers,
                job_payload=json.loads(str(telegram_due["payload_json"])),
                job_id=str(telegram_due["job_id"]),
                job_owner_id=job_owner_id,
                now=instant,
            )
            return PublicationTick(result.state, result)
        return PublicationTick("idle")
