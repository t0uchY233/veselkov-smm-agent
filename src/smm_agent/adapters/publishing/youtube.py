"""YouTube payload boundary; real Data API transport is Slice 6."""

from smm_agent.contracts.publication import PublicationRequest


def youtube_request(
    *,
    release_id: str,
    schedule_key: str,
    payload_sha256: str,
    payload: dict[str, object],
) -> PublicationRequest:
    return PublicationRequest(
        release_id=release_id,
        platform="youtube",
        idempotency_key=f"publication:{release_id}:youtube:{schedule_key}",
        payload_sha256=payload_sha256,
        payload=payload,
    )
