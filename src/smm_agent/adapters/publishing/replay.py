"""Deterministic in-memory provider used by contract and GC-03 tests."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from smm_agent.contracts.publication import (
    Platform,
    PreparedPublication,
    PublicationRequest,
    PublicationSnapshot,
)


class ReplayProviderError(RuntimeError):
    pass


class ReplayPublisher:
    def __init__(self, platform: Platform, *, state_path: Path | None = None) -> None:
        self.platform = platform
        self.state_path = state_path
        self._items: dict[str, PublicationSnapshot] = {}
        self._by_key: dict[str, str] = {}
        self._failures: set[str] = set()
        self._operations: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self._load()

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.is_file():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._by_key = {str(key): str(value) for key, value in payload["by_key"].items()}
        self._operations = {
            str(key): str(value) for key, value in payload.get("operations", {}).items()
        }
        self._items = {
            str(remote_id): PublicationSnapshot.model_validate(snapshot)
            for remote_id, snapshot in payload["items"].items()
        }

    def _flush(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "by_key": self._by_key,
                    "operations": self._operations,
                    "items": {
                        remote_id: snapshot.model_dump(mode="json")
                        for remote_id, snapshot in self._items.items()
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def fail(self, operation: str) -> None:
        self._failures.add(operation)

    def recover(self, operation: str) -> None:
        self._failures.discard(operation)

    def _check(self, operation: str) -> None:
        if operation in self._failures:
            raise ReplayProviderError(f"replay {self.platform} {operation} failed")

    def prepare(self, request: PublicationRequest) -> PreparedPublication:
        self._check("prepare")
        self.calls.append(("prepare", request.idempotency_key))
        existing_id = self._by_key.get(request.idempotency_key)
        if existing_id:
            existing = self._items[existing_id]
            if existing.payload_sha256 != request.payload_sha256:
                raise ReplayProviderError("idempotency key reused with another payload")
            return self._prepared(existing)
        remote_id = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:16]
        known_url = {
            "youtube": f"https://youtu.be/{remote_id}",
            "dzen": f"https://dzen.ru/a/{remote_id}",
            "telegram": None,
        }[self.platform]
        snapshot = PublicationSnapshot(
            platform=self.platform,
            state="prepared",
            remote_id=remote_id,
            known_url=known_url,
            payload_sha256=request.payload_sha256,
        )
        self._items[remote_id] = snapshot
        self._by_key[request.idempotency_key] = remote_id
        self._flush()
        return self._prepared(snapshot)

    @staticmethod
    def _prepared(snapshot: PublicationSnapshot) -> PreparedPublication:
        return PreparedPublication(
            platform=snapshot.platform,
            state=snapshot.state,
            remote_id=snapshot.remote_id,
            known_url=snapshot.known_url,
            payload_sha256=snapshot.payload_sha256,
        )

    def preflight(self, remote_id: str, *, payload_sha256: str) -> None:
        self._check("preflight")
        self.calls.append(("preflight", remote_id))
        item = self._items[remote_id]
        if item.payload_sha256 != payload_sha256 or item.state not in {"prepared", "armed"}:
            raise ReplayProviderError("remote payload/state mismatch")

    def arm(
        self, remote_id: str, *, target_at_utc: datetime, operation_key: str
    ) -> PublicationSnapshot:
        self._check("arm")
        self.calls.append(("arm", operation_key))
        item = self._items[remote_id]
        if operation_key in self._operations:
            return item
        if item.state != "prepared":
            raise ReplayProviderError(f"cannot arm publication in {item.state}")
        updated = item.model_copy(
            update={"state": "armed", "target_at_utc": target_at_utc.astimezone(UTC)}
        )
        self._items[remote_id] = updated
        self._operations[operation_key] = remote_id
        self._flush()
        return updated

    def status(
        self, remote_id: str, *, now: datetime | None = None
    ) -> PublicationSnapshot:
        self._check("status")
        self.calls.append(("status", remote_id))
        item = self._items[remote_id]
        if (
            self.platform in {"youtube", "dzen"}
            and now is not None
            and item.state == "armed"
            and item.target_at_utc is not None
            and now.astimezone(UTC) >= item.target_at_utc.astimezone(UTC)
        ):
            item = item.model_copy(
                update={"state": "public", "public_at": now.astimezone(UTC)}
            )
            self._items[remote_id] = item
            self._flush()
        return item

    def cancel(self, remote_id: str, *, operation_key: str) -> PublicationSnapshot:
        self._check("cancel")
        self.calls.append(("cancel", operation_key))
        item = self._items[remote_id]
        if operation_key in self._operations or item.state == "cancelled":
            return item
        if item.state == "public":
            raise ReplayProviderError("public publication cannot be cancelled")
        updated = item.model_copy(update={"state": "cancelled"})
        self._items[remote_id] = updated
        self._operations[operation_key] = remote_id
        self._flush()
        return updated

    def execute(
        self, remote_id: str, *, operation_key: str, now: datetime
    ) -> PublicationSnapshot:
        self._check("execute")
        self.calls.append(("execute", operation_key))
        item = self._items[remote_id]
        if operation_key in self._operations or item.state == "public":
            return item
        if item.state != "armed" or item.target_at_utc is None:
            raise ReplayProviderError(f"cannot execute publication in {item.state}")
        if now.astimezone(UTC) < item.target_at_utc.astimezone(UTC):
            raise ReplayProviderError("publication target has not arrived")
        updated = item.model_copy(
            update={"state": "public", "public_at": now.astimezone(UTC)}
        )
        self._items[remote_id] = updated
        self._operations[operation_key] = remote_id
        self._flush()
        return updated

    def make_public(self, remote_id: str, *, public_at: datetime) -> PublicationSnapshot:
        item = self._items[remote_id]
        updated = item.model_copy(
            update={"state": "public", "public_at": public_at.astimezone(UTC)}
        )
        self._items[remote_id] = updated
        self._flush()
        return updated
