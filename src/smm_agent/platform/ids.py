"""Identifiers shared by persistence-facing application services."""

import secrets
import time


def uuid7() -> str:
    """Generate an RFC 9562 UUIDv7 without a third-party dependency."""
    timestamp_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(74)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((random_bits >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= random_bits & ((1 << 62) - 1)
    hex_value = f"{value:032x}"
    return (
        f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-"
        f"{hex_value[16:20]}-{hex_value[20:]}"
    )
