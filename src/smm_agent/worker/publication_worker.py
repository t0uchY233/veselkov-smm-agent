"""One fenced polling step for coordinated publication and recovery."""

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from smm_agent.application.notification_service import deliver_notification
from smm_agent.application.publication_service import (
    execute_telegram_task,
    preflight_scheduled_release,
    schedule_release,
)
from smm_agent.application.recovery_service import (
    RecoveryOutcome,
    reconcile_publication_recovery,
)
from smm_agent.contracts.publication import (
    NotificationJobPayload,
    Platform,
    PublicationResult,
    RecoveryJobPayload,
)
from smm_agent.domain.notification.ports import AlertTransport
from smm_agent.domain.publication.ports import Publisher
from smm_agent.platform.db import Database
from smm_agent.platform.ids import uuid7
from smm_agent.platform.incidents import IncidentStore
from smm_agent.platform.jobs import JobClaim, JobStore

_HEARTBEAT_SECONDS = 20


def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _LeaseHeartbeat:
    """Renew one claimed job while an external provider call is in progress.

    SQLite mutations are fenced by ``JobClaim``. A worker which loses that
    fence does not try to finish or retry the job; its caller observes
    ``stale`` and leaves the replacement worker as the sole authority.
    """

    def __init__(
        self,
        *,
        database: Database,
        claim: JobClaim,
        interval_seconds: float = _HEARTBEAT_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Интервал heartbeat должен быть положительным.")
        self.database = database
        self.claim = claim
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._stale = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stale(self) -> bool:
        return self._stale.is_set()

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name=f"smm-job-heartbeat-{self.claim.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: object, exc_value: object, traceback: object
    ) -> Literal[False]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_seconds + 1)
        # Provider exceptions, including KeyboardInterrupt/SystemExit, remain
        # visible to the supervisor so crash recovery can reconcile receipts.
        return False

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self.database.transaction() as connection:
                    renewed = JobStore.heartbeat(
                        connection,
                        claim=self.claim,
                        now=_iso(datetime.now(UTC)),
                    )
            except Exception:
                # A heartbeat cannot safely repair a DB/provider failure from a
                # background thread. The active call will finish fenced or be
                # recovered once this lease expires.
                self._stale.set()
                return
            if not renewed:
                self._stale.set()
                return


@dataclass(frozen=True, slots=True)
class PublicationTick:
    outcome: str
    publication: PublicationResult | None = None


class PublicationWorker:
    def __init__(
        self,
        *,
        database: Database,
        publishers: dict[Platform, Publisher],
        alert_transport: AlertTransport | None = None,
        worker_id: str | None = None,
        heartbeat_interval_seconds: float = _HEARTBEAT_SECONDS,
    ) -> None:
        self.database = database
        self.publishers = publishers
        self.alert_transport = alert_transport
        self.worker_id = worker_id or uuid7()
        self.heartbeat_interval_seconds = heartbeat_interval_seconds

    @staticmethod
    def _command_id(
        kind: str, release_id: str, revision: int, attempt_id: str | None = None
    ) -> str:
        material = f"{kind}:{release_id}:{revision}:{attempt_id or 'stable'}"
        digest = hashlib.sha256(material.encode()).hexdigest()
        return f"worker-{digest[:32]}"

    def _claim(self, *, release_id: str, kind: str, now: datetime) -> JobClaim | None:
        with self.database.transaction() as connection:
            return JobStore.claim_due(
                connection,
                release_id=release_id,
                kind=kind,
                worker_id=self.worker_id,
                now=_iso(now),
            )

    def _heartbeat(self, claim: JobClaim) -> _LeaseHeartbeat:
        return _LeaseHeartbeat(
            database=self.database,
            claim=claim,
            interval_seconds=self.heartbeat_interval_seconds,
        )

    def _complete(self, claim: JobClaim, *, now: datetime) -> bool:
        with self.database.transaction() as connection:
            return JobStore.mark_succeeded(connection, claim=claim, now=_iso(now))

    def _run_preflight(
        self, *, claim: JobClaim, revision: int, now: datetime
    ) -> PublicationTick:
        with self._heartbeat(claim) as heartbeat:
            result = preflight_scheduled_release(
                self.database,
                command_id=self._command_id(
                    "preflight", claim.release_id, revision, claim.attempt_id
                ),
                expected_revision=revision,
                publishers=self.publishers,
                now=now,
            )
        # A failed preflight delegates its own job disposition to the
        # cancellation transaction.  In particular, that transaction can
        # cancel this running job before moving the release to
        # ``needs_attention``; trying to complete it afterwards would turn a
        # correct terminal outcome into a misleading stale tick.
        if result.state != "scheduled":
            return PublicationTick(result.state, result)
        if heartbeat.stale or not self._complete(claim, now=now):
            return PublicationTick("stale", result)
        outcome = "preflight_ok" if result.state == "scheduled" else result.state
        return PublicationTick(outcome, result)

    def _run_telegram_target(
        self, *, claim: JobClaim, revision: int, now: datetime
    ) -> PublicationTick:
        with self._heartbeat(claim):
            result = execute_telegram_task(
                self.database,
                command_id=self._command_id(
                    "telegram", claim.release_id, revision, claim.attempt_id
                ),
                expected_revision=revision,
                publishers=self.publishers,
                claim=claim,
                now=now,
            )
        return PublicationTick(result.state, result)

    def _run_recovery(self, *, claim: JobClaim, now: datetime) -> PublicationTick:
        payload = RecoveryJobPayload.model_validate_json(claim.payload_json)

        def record_terminal(connection: sqlite3.Connection, outcome: RecoveryOutcome) -> None:
            if outcome.error is None:
                raise RuntimeError("Terminal recovery outcome не содержит ошибки.")
            IncidentStore.open_with_notification(
                connection,
                release_id=outcome.release_id,
                job_id=outcome.job_id,
                platform=payload.platform,
                error=outcome.error,
                safe_next_action=(
                    "Проверить доступ к площадке и повторно запустить "
                    "восстановление после исправления."
                ),
                now=now,
            )

        with self._heartbeat(claim):
            outcome = reconcile_publication_recovery(
                self.database,
                claim=claim,
                publishers=self.publishers,
                now=now,
                terminal_recorder=record_terminal,
            )
        return PublicationTick(outcome.state)

    def _run_notification(self, *, claim: JobClaim, now: datetime) -> PublicationTick:
        if self.alert_transport is None:
            raise RuntimeError("Для notification job не настроен AlertTransport.")
        payload = NotificationJobPayload.model_validate_json(claim.payload_json)
        with self._heartbeat(claim):
            outcome = deliver_notification(
                self.database,
                claim=claim,
                notification_id=payload.notification_id,
                transport=self.alert_transport,
                now=now,
            )
        return PublicationTick(f"notification_{outcome.outcome}")

    def run_once(self, *, now: datetime | None = None) -> PublicationTick:
        instant = (now or datetime.now(UTC)).astimezone(UTC)
        with self.database.connect() as connection:
            release = self.database.active_release(connection)
        if release is None:
            return PublicationTick("idle")

        if release.state == "publication_preparing":
            result = schedule_release(
                self.database,
                command_id=self._command_id("schedule", release.release_id, release.revision),
                expected_revision=release.revision,
                publishers=self.publishers,
                now=instant,
            )
            return PublicationTick(result.state, result)

        eligible_kinds: tuple[str, ...]
        if release.state == "scheduled":
            eligible_kinds = ("publication_preflight", "telegram_publish_reconcile")
        elif release.state == "recovering":
            eligible_kinds = ("publication_recovery",)
        elif release.state == "needs_attention":
            # Cancellation reconciliation stays human-controlled until its
            # separate status-first policy is implemented. It must never be
            # accidentally treated as an ordinary retry after a public receipt.
            eligible_kinds = ("notification_deliver",)
        else:
            return PublicationTick("idle")

        for kind in eligible_kinds:
            claim = self._claim(release_id=release.release_id, kind=kind, now=instant)
            if claim is None:
                continue
            if kind == "publication_preflight":
                return self._run_preflight(claim=claim, revision=release.revision, now=instant)
            if kind == "telegram_publish_reconcile":
                return self._run_telegram_target(
                    claim=claim, revision=release.revision, now=instant
                )
            if kind == "publication_recovery":
                return self._run_recovery(claim=claim, now=instant)
            return self._run_notification(claim=claim, now=instant)
        return PublicationTick("idle")
