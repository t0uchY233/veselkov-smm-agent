"""Versioned contracts for recording ingest, alignment, rendering, and QC."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VideoContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaProbe(VideoContract):
    duration_seconds: float = Field(gt=0)
    video_streams: int = Field(ge=0)
    audio_streams: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: float = Field(gt=0)
    real_frame_rate: float | None = Field(default=None, gt=0)
    audio_sample_rate: int | None = Field(default=None, gt=0)
    video_codec: str | None = None
    audio_codec: str | None = None


class WordTiming(VideoContract):
    word: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def end_follows_start(self) -> "WordTiming":
        if self.end <= self.start:
            raise ValueError("word end must follow start")
        return self


class Transcript(VideoContract):
    schema_version: Literal["1.0"] = "1.0"
    language: str = Field(min_length=2)
    words: list[WordTiming] = Field(min_length=1)

    @model_validator(mode="after")
    def words_are_monotonic(self) -> "Transcript":
        if any(
            left.start > right.start
            for left, right in zip(self.words, self.words[1:], strict=False)
        ):
            raise ValueError("transcript words must be monotonic")
        return self


class AlignmentProfile(VideoContract):
    schema_version: Literal["1.0"] = "1.0"
    asr_runtime: str = Field(min_length=1)
    asr_argv_template: list[str] = Field(min_length=2)
    asr_executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ffmpeg_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ffprobe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    author_focal_x: float = Field(ge=0, le=1)
    author_focal_y: float = Field(ge=0, le=1)
    confidence_threshold: float = Field(ge=0.5, le=0.99)
    accepted_by: Literal["operator"]
    accepted_at: datetime

    @model_validator(mode="after")
    def acceptance_is_timezone_aware(self) -> "AlignmentProfile":
        if self.accepted_at.tzinfo is None:
            raise ValueError("alignment profile acceptance time must include timezone")
        if not any("{source}" in part for part in self.asr_argv_template):
            raise ValueError("ASR argv template must contain {source}")
        return self


class MediaProfileAcceptance(VideoContract):
    schema_version: Literal["1.0"] = "1.0"
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_by: Literal["operator"]
    accepted_at: datetime


class AnchorMatch(VideoContract):
    visual_id: str
    anchor_text: str
    timestamp: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    transcript_start_index: int = Field(ge=0)
    transcript_end_index: int = Field(ge=0)


class TimelineEntry(VideoContract):
    visual_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def end_follows_start(self) -> "TimelineEntry":
        if self.end <= self.start:
            raise ValueError("timeline end must follow start")
        return self


class AlignmentResult(VideoContract):
    schema_version: Literal["1.0"] = "1.0"
    confidence_threshold: float = Field(gt=0, le=1)
    minimum_confidence: float = Field(ge=0, le=1)
    fullscreen_until: float = Field(ge=0)
    matches: list[AnchorMatch] = Field(min_length=1, max_length=5)
    timeline: list[TimelineEntry] = Field(min_length=1, max_length=5)


class VideoQc(VideoContract):
    schema_version: Literal["1.0"] = "1.0"
    passed: bool
    duration_seconds: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_codec: str
    audio_codec: str
    decoded: bool
    size_bytes: int = Field(ge=0)
    checks: dict[str, bool]
