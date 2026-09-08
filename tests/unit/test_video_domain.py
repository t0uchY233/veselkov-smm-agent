from pathlib import Path

import pytest

from smm_agent.contracts.video import MediaProbe, Transcript, WordTiming
from smm_agent.domain.video.service import (
    AlignmentLowConfidence,
    build_timeline,
    validate_recording,
)


def transcript(*phrases: tuple[float, str, float]) -> Transcript:
    words: list[WordTiming] = []
    for start, phrase, confidence in phrases:
        for offset, word in enumerate(phrase.lower().split()):
            words.append(
                WordTiming(
                    word=word,
                    start=start + offset * 0.25,
                    end=start + (offset + 1) * 0.25,
                    confidence=confidence,
                )
            )
    return Transcript(words=words, language="ru")


def test_timeline_is_continuous_from_first_anchor_to_video_end() -> None:
    result = build_timeline(
        anchors=[
            ("visual-1", "акт фиксирует долг"),
            ("visual-2", "срок запускает оплату"),
            ("visual-3", "деньги возвращаются в оборот"),
        ],
        transcript=transcript(
            (12.0, "акт фиксирует долг", 0.97),
            (110.0, "срок запускает оплату", 0.95),
            (250.0, "деньги возвращаются в оборот", 0.96),
        ),
        duration_seconds=450.0,
        confidence_threshold=0.85,
    )

    assert [(item.start, item.end) for item in result.timeline] == [
        (12.0, 110.0),
        (110.0, 250.0),
        (250.0, 450.0),
    ]
    assert result.fullscreen_until == 12.0
    assert result.minimum_confidence >= 0.85


def test_low_confidence_anchor_blocks_timeline() -> None:
    with pytest.raises(AlignmentLowConfidence) as captured:
        build_timeline(
            anchors=[
                ("visual-1", "акт фиксирует долг"),
                ("visual-2", "срок запускает оплату"),
                ("visual-3", "деньги возвращаются в оборот"),
            ],
            transcript=transcript(
                (20.0, "акт фиксирует долг", 0.41),
                (120.0, "срок запускает оплату", 0.97),
                (240.0, "деньги возвращаются в оборот", 0.97),
            ),
            duration_seconds=450.0,
            confidence_threshold=0.85,
        )

    assert captured.value.unmatched_anchors == ["visual-1"]


def test_alignment_uses_first_confident_repetition() -> None:
    result = build_timeline(
        anchors=[
            ("visual-1", "акт фиксирует долг"),
            ("visual-2", "срок запускает оплату"),
            ("visual-3", "деньги возвращаются в оборот"),
        ],
        transcript=transcript(
            (20.0, "акт фиксирует долг", 0.90),
            (80.0, "акт фиксирует долг", 0.99),
            (180.0, "срок запускает оплату", 0.98),
            (330.0, "деньги возвращаются в оборот", 0.98),
        ),
        duration_seconds=450.0,
        confidence_threshold=0.85,
    )

    assert result.timeline[0].start == 20.0


@pytest.mark.parametrize("duration", [299.999, 600.001])
def test_recording_duration_outside_five_to_ten_minutes_is_rejected(duration: float) -> None:
    probe = MediaProbe(
        duration_seconds=duration,
        video_streams=1,
        audio_streams=1,
        width=1920,
        height=1080,
        frame_rate=30.0,
    )

    with pytest.raises(ValueError, match="300.000.*600.000"):
        validate_recording(Path("recording.mp4"), probe)


def test_recording_requires_one_video_and_audio_stream() -> None:
    probe = MediaProbe(
        duration_seconds=450.0,
        video_streams=1,
        audio_streams=0,
        width=1920,
        height=1080,
        frame_rate=30.0,
    )

    with pytest.raises(ValueError, match="аудиопоток"):
        validate_recording(Path("recording.mp4"), probe)
