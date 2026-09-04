"""Pure publication invariants shared by every provider adapter."""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from smm_agent.contracts.publication import Platform, PreparedPublication, PublicationSnapshot


class PublicationInvariantFailed(ValueError):
    pass


def validate_prepared_snapshot(
    snapshot: PreparedPublication, *, platform: Platform, payload_sha256: str
) -> None:
    if snapshot.platform != platform:
        raise PublicationInvariantFailed("Provider вернул другую площадку.")
    if snapshot.state != "prepared":
        raise PublicationInvariantFailed(f"{platform} не подтвердил prepared state.")
    if snapshot.payload_sha256 != payload_sha256:
        raise PublicationInvariantFailed(f"{platform} вернул другой payload hash.")


def validate_link_sources(
    youtube: PreparedPublication,
    dzen: PublicationSnapshot,
    *,
    target_at_utc: datetime,
) -> tuple[str, str]:
    if youtube.platform != "youtube" or youtube.state not in {"prepared", "armed"}:
        raise PublicationInvariantFailed("YouTube должен быть prepared или armed.")
    if dzen.platform != "dzen" or dzen.state != "armed":
        raise PublicationInvariantFailed("Дзен должен подтвердить scheduled material.")
    if dzen.target_at_utc is None or dzen.target_at_utc.astimezone(UTC) != target_at_utc:
        raise PublicationInvariantFailed("Дзен подтвердил другой target.")
    youtube_known_url = youtube.known_url
    youtube_url = urlparse(youtube_known_url or "")
    if youtube_url.scheme != "https" or youtube_url.hostname not in {
        "youtu.be",
        "youtube.com",
        "www.youtube.com",
    }:
        raise PublicationInvariantFailed("YouTube не вернул стабильную HTTPS-ссылку.")
    if not dzen.known_url or not dzen.known_url.startswith("https://dzen.ru/"):
        raise PublicationInvariantFailed("Дзен не вернул ссылку отложенного материала.")
    if youtube_known_url is None:
        raise PublicationInvariantFailed("YouTube не вернул стабильную HTTPS-ссылку.")
    return youtube_known_url, dzen.known_url


def validate_future_target(target: datetime, now: datetime) -> datetime:
    if target.tzinfo is None or now.tzinfo is None:
        raise PublicationInvariantFailed("Target и текущее время должны иметь timezone.")
    normalized = target.astimezone(UTC)
    if normalized <= now.astimezone(UTC):
        raise PublicationInvariantFailed("Target публикации уже наступил.")
    if normalized - now.astimezone(UTC) < timedelta(minutes=35):
        raise PublicationInvariantFailed("До target должно оставаться не менее 35 минут.")
    return normalized


def validate_armed_snapshot(
    snapshot: PublicationSnapshot,
    *,
    platform: Platform,
    payload_sha256: str,
    target_at_utc: datetime,
) -> None:
    if snapshot.platform != platform:
        raise PublicationInvariantFailed("Provider вернул другую площадку.")
    if snapshot.state != "armed":
        raise PublicationInvariantFailed(f"{platform} не подтвердил armed state.")
    if snapshot.payload_sha256 != payload_sha256:
        raise PublicationInvariantFailed(f"{platform} вернул другой payload hash.")
    if snapshot.target_at_utc is None or snapshot.target_at_utc.astimezone(UTC) != target_at_utc:
        raise PublicationInvariantFailed(f"{platform} подтвердил другой target.")


def validate_public_snapshot(
    snapshot: PublicationSnapshot,
    *,
    platform: Platform,
    payload_sha256: str,
    target_at_utc: datetime,
) -> None:
    if snapshot.platform != platform or snapshot.state != "public":
        raise PublicationInvariantFailed(f"{platform} не подтвердил public state.")
    if snapshot.payload_sha256 != payload_sha256:
        raise PublicationInvariantFailed(f"{platform} вернул другой payload hash.")
    if snapshot.target_at_utc is None or snapshot.target_at_utc.astimezone(UTC) != target_at_utc:
        raise PublicationInvariantFailed(f"{platform} вернул другой target.")
    if snapshot.public_at is None:
        raise PublicationInvariantFailed(f"{platform} не вернул public_at.")


def validate_telegram_caption(caption: str) -> None:
    if "{{dzen_url}}" in caption or "{{youtube_url}}" in caption:
        raise PublicationInvariantFailed("В Telegram caption остались link slots.")
    if len(caption) > 1000 or len(caption.encode("utf-16-le")) // 2 > 1000:
        raise PublicationInvariantFailed("Telegram caption превышает 1000 символов.")
