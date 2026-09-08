"""Ports for deterministic editorial representations."""

from typing import Protocol

from smm_agent.contracts.editorial import EditorialBundle


class DocxBuilder(Protocol):
    def build(self, bundle: EditorialBundle, assets: dict[str, bytes]) -> bytes: ...


class ImageInspector(Protocol):
    def dimensions(self, payload: bytes) -> tuple[int, int]: ...

    def contrast_score(self, payload: bytes) -> float: ...
