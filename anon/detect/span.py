"""Detection result type shared by all detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# kinds that name an entity (letters / representative stars)
NAME_KINDS = {"person", "org", "rep_block"}
# value kinds -> reviewer category
VALUE_CATEGORY = {
    "place": "place",
    "place_adj": "place",
    "street": "place",
    "address": "place",
    "address_tail": "place",
    "fieldname": "place",
    "plot": "plot",
    "birthdate": "id",
    "id": "id",
    "product": "id",
}
KEEP_KIND = "keep"


@dataclass
class Span:
    part: str
    para: int
    start: int  # original character offsets in the paragraph text
    end: int
    text: str
    kind: str
    source: str  # rubrum | rules | gazetteer | ner | llm | rescan | keep | reviewer
    confidence: float
    role: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, int]:
        return (self.part, self.para)

    def overlaps(self, other: "Span") -> bool:
        return self.key == other.key and self.start < other.end and other.start < self.end

    def contains(self, other: "Span") -> bool:
        return self.key == other.key and self.start <= other.start and other.end <= self.end

    def same_extent(self, other: "Span") -> bool:
        return self.key == other.key and self.start == other.start and self.end == other.end

    def __len__(self) -> int:
        return self.end - self.start
