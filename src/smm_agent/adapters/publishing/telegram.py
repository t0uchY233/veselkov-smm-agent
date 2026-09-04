"""Telegram payload boundary; Bot API and Task Scheduler transports are Slice 6."""

from smm_agent.contracts.publication import PublicationRequest
from smm_agent.domain.publication.service import validate_telegram_caption


def telegram_request(
    *,
    release_id: str,
    schedule_key: str,
    payload_sha256: str,
    payload: dict[str, object],
) -> PublicationRequest:
    caption = payload.get("caption")
    if not isinstance(caption, str):
        raise ValueError("Telegram payload должен содержать caption.")
    validate_telegram_caption(caption)
    return PublicationRequest(
        release_id=release_id,
        platform="telegram",
        idempotency_key=f"publication:{release_id}:telegram:{schedule_key}",
        payload_sha256=payload_sha256,
        payload=payload,
    )
