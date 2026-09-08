"""Pure validation and speech-to-visual alignment rules for UC-06/07."""

import re
from difflib import SequenceMatcher
from pathlib import Path

from smm_agent.contracts.video import (
    AlignmentResult,
    AnchorMatch,
    MediaProbe,
    TimelineEntry,
    Transcript,
)

ALLOWED_RECORDING_SUFFIXES = {".mp4", ".mov", ".mkv"}


class AlignmentLowConfidence(Exception):
    def __init__(self, unmatched_anchors: list[str]) -> None:
        self.unmatched_anchors = unmatched_anchors
        super().__init__("anchors with insufficient confidence: " + ", ".join(unmatched_anchors))


def validate_recording(path: Path, probe: MediaProbe) -> None:
    if path.suffix.lower() not in ALLOWED_RECORDING_SUFFIXES:
        raise ValueError("Разрешены только MP4, MOV и MKV.")
    if probe.video_streams != 1:
        raise ValueError("Запись должна содержать ровно один видеопоток.")
    if probe.audio_streams < 1:
        raise ValueError("Запись должна содержать минимум один аудиопоток.")
    if not 300.0 <= probe.duration_seconds <= 600.0:
        raise ValueError("Длительность записи должна быть от 300.000 до 600.000 секунд.")


def _tokens(value: str) -> list[str]:
    return re.findall(r"[0-9a-zа-яё]+", value.casefold())


def _window_score(expected: list[str], actual: list[str], confidences: list[float]) -> float:
    lexical = SequenceMatcher(None, expected, actual).ratio()
    acoustic = sum(confidences) / len(confidences)
    return lexical * acoustic


def build_timeline(
    *,
    anchors: list[tuple[str, str]],
    transcript: Transcript,
    duration_seconds: float,
    confidence_threshold: float,
) -> AlignmentResult:
    if not 0 < confidence_threshold <= 1:
        raise ValueError("Порог alignment должен быть в диапазоне (0, 1].")
    if not 3 <= len(anchors) <= 5:
        raise ValueError("Для монтажа требуется от трёх до пяти anchors.")

    transcript_tokens = [
        _tokens(word.word)[0] if _tokens(word.word) else "" for word in transcript.words
    ]
    matches: list[AnchorMatch] = []
    cursor = 0
    unmatched: list[str] = []

    for visual_id, anchor_text in anchors:
        expected = _tokens(anchor_text)
        if not expected:
            unmatched.append(visual_id)
            continue
        selected: tuple[float, int, int] | None = None
        minimum_window = max(1, len(expected) - 1)
        maximum_window = min(len(expected) + 1, len(transcript_tokens) - cursor)
        for start in range(cursor, len(transcript_tokens) - minimum_window + 1):
            candidates: list[tuple[float, int, int]] = []
            for width in range(minimum_window, maximum_window + 1):
                end = start + width
                if end > len(transcript_tokens):
                    continue
                score = _window_score(
                    expected,
                    transcript_tokens[start:end],
                    [word.confidence for word in transcript.words[start:end]],
                )
                candidates.append((score, start, end))
            best_here = max(candidates, default=None)
            if best_here and best_here[0] >= confidence_threshold:
                selected = best_here
                break
        if selected is None:
            unmatched.append(visual_id)
            continue
        score, start, end = selected
        matches.append(
            AnchorMatch(
                visual_id=visual_id,
                anchor_text=anchor_text,
                timestamp=transcript.words[start].start,
                confidence=score,
                transcript_start_index=start,
                transcript_end_index=end - 1,
            )
        )
        cursor = end

    if unmatched or len(matches) != len(anchors):
        raise AlignmentLowConfidence(unmatched or [item[0] for item in anchors[len(matches) :]])
    timestamps = [match.timestamp for match in matches]
    if any(left >= right for left, right in zip(timestamps, timestamps[1:], strict=False)):
        raise AlignmentLowConfidence([match.visual_id for match in matches])
    if timestamps[-1] >= duration_seconds:
        raise AlignmentLowConfidence([matches[-1].visual_id])

    timeline = [
        TimelineEntry(
            visual_id=match.visual_id,
            start=match.timestamp,
            end=matches[index + 1].timestamp if index + 1 < len(matches) else duration_seconds,
        )
        for index, match in enumerate(matches)
    ]
    return AlignmentResult(
        confidence_threshold=confidence_threshold,
        minimum_confidence=min(match.confidence for match in matches),
        fullscreen_until=matches[0].timestamp,
        matches=matches,
        timeline=timeline,
    )
