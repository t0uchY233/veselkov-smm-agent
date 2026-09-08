"""Validated editorial imports produced by Codex."""

import hashlib
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanDocument(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=5, max_length=180)
    thesis: str = Field(min_length=10, max_length=500)
    sections: list[str] = Field(min_length=3, max_length=12)
    target_duration_minutes: float = Field(ge=5, le=10)


class SourceInput(StrictModel):
    source_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    url: HttpUrl
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=200)
    checked_at: AwareDatetime
    evidence_excerpt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClaimInput(StrictModel):
    claim_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    exact_text: str = Field(min_length=3)
    materiality: Literal["material", "context"]
    status: Literal["supported", "opinion", "conflict"]
    source_ids: list[str] = Field(default_factory=list)


class VisualInput(StrictModel):
    visual_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    kind: Literal["image", "chart", "table", "dashboard"]
    anchor_text: str = Field(min_length=5)
    purpose: str = Field(min_length=10)
    asset_path: str
    caption: str = Field(min_length=1, max_length=300)
    claim_ids: list[str] = Field(default_factory=list)


class ReactionInput(StrictModel):
    emoji: str = Field(min_length=1, max_length=8)
    text: str = Field(min_length=2, max_length=120)


class TelegramInput(StrictModel):
    title: str = Field(min_length=5, max_length=180)
    lead: str = Field(min_length=10, max_length=500)
    steps: list[str] = Field(min_length=3, max_length=5)
    cta: str = Field(min_length=20, max_length=500)
    reactions: list[ReactionInput] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_link_slots(self) -> "TelegramInput":
        for slot in ("{{dzen_url}}", "{{youtube_url}}"):
            if self.cta.count(slot) != 1:
                raise ValueError(f"Telegram CTA должен содержать ровно один slot {slot}")
        return self

    def render_template(self) -> str:
        steps = "\n".join(f"→ {step}" for step in self.steps)
        reactions = "\n".join(f"{item.emoji} - {item.text}" for item in self.reactions)
        return f"{self.title}\n\n{self.lead}\n\n{steps}\n\n{self.cta}\n\n{reactions}"


class YouTubeInput(StrictModel):
    title: str = Field(min_length=5, max_length=100)
    description: str = Field(min_length=10, max_length=5000)
    tags: list[str] = Field(min_length=1, max_length=20)


class DzenInput(StrictModel):
    title: str = Field(min_length=5, max_length=180)


class HumanizerEvidence(StrictModel):
    skill_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    tone_of_voice_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stages: list[str]
    tool_types: list[str] = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_count: int = Field(ge=0)


class EditorialBundle(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    main_text: str = Field(min_length=100)
    sources: list[SourceInput] = Field(min_length=1)
    claims: list[ClaimInput] = Field(min_length=1)
    visuals: list[VisualInput] = Field(min_length=3, max_length=5)
    cover_path: str
    telegram: TelegramInput
    youtube: YouTubeInput
    dzen: DzenInput
    humanizer: HumanizerEvidence

    @model_validator(mode="after")
    def validate_editorial_invariants(self) -> "EditorialBundle":
        source_ids = [item.source_id for item in self.sources]
        claim_ids = [item.claim_id for item in self.claims]
        visual_ids = [item.visual_id for item in self.visuals]
        visual_paths = [item.asset_path for item in self.visuals]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id должны быть уникальны")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("claim_id должны быть уникальны")
        if len(visual_ids) != len(set(visual_ids)):
            raise ValueError("visual_id должны быть уникальны")
        if len(visual_paths) != len(set(visual_paths)) or self.cover_path in visual_paths:
            raise ValueError("Обложка и визуалы должны использовать разные asset paths")

        known_sources = set(source_ids)
        for claim in self.claims:
            if not set(claim.source_ids) <= known_sources:
                raise ValueError(f"Claim {claim.claim_id} ссылается на неизвестный источник")
            if claim.materiality == "material" and (
                claim.status != "supported" or not claim.source_ids
            ):
                raise ValueError(f"Существенный claim {claim.claim_id} должен иметь источник")

        known_claims = set(claim_ids)
        last_position = -1
        anchors: set[str] = set()
        for visual in self.visuals:
            if visual.anchor_text in anchors:
                raise ValueError("Anchors визуалов должны быть уникальны")
            anchors.add(visual.anchor_text)
            if self.main_text.count(visual.anchor_text) != 1:
                raise ValueError(
                    f"Anchor {visual.anchor_text!r} должен встретиться в тексте один раз"
                )
            position = self.main_text.index(visual.anchor_text)
            if position <= last_position:
                raise ValueError("Visual anchors должны идти в порядке Основного текста")
            last_position = position
            if not set(visual.claim_ids) <= known_claims:
                raise ValueError(f"Visual {visual.visual_id} ссылается на неизвестный claim")

        word_count = len(re.findall(r"\b[\w-]+\b", self.main_text, flags=re.UNICODE))
        duration = word_count / 86
        if not 5 <= duration <= 10:
            raise ValueError(
                f"Оценка длительности {duration:.1f} мин вне диапазона 5-10 минут"
            )

        digest = hashlib.sha256(self.main_text.encode("utf-8")).hexdigest()
        if self.humanizer.output_sha256 != digest:
            raise ValueError("Humanizer output hash не совпадает с Основным текстом")
        if self.humanizer.error_count != 0:
            raise ValueError("Humanizer lint содержит ошибки")
        if self.humanizer.stages != ["evidence_draft", "tone_of_voice", "humanizer", "lint"]:
            raise ValueError("Нарушен порядок evidence draft, TOV, Humanize и lint")

        template = self.telegram.render_template()
        rendered = template.replace("{{dzen_url}}", "https://dzen.ru/a/" + "x" * 64).replace(
            "{{youtube_url}}", "https://youtu.be/" + "x" * 32
        )
        if len(rendered) > 1000 or len(rendered.encode("utf-16-le")) // 2 > 1000:
            raise ValueError("Telegram caption не помещается в 1000 символов после ссылок")
        return self
