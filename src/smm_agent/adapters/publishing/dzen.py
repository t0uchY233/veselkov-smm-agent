"""Dzen payload boundary; real Playwright page objects are Slice 6."""

from smm_agent.contracts.publication import PublicationRequest


def dzen_request(
    *,
    release_id: str,
    schedule_key: str,
    payload_sha256: str,
    payload: dict[str, object],
) -> PublicationRequest:
    return PublicationRequest(
        release_id=release_id,
        platform="dzen",
        idempotency_key=f"publication:{release_id}:dzen:{schedule_key}",
        payload_sha256=payload_sha256,
        payload=payload,
    )
