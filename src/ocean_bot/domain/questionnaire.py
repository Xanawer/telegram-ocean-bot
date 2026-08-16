from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Trait(StrEnum):
    OPENNESS = "openness"
    CONSCIENTIOUSNESS = "conscientiousness"
    EXTRAVERSION = "extraversion"
    AGREEABLENESS = "agreeableness"
    NEUROTICISM = "neuroticism"


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    text: str
    trait: Trait
    reverse: bool
    facet: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.text:
            raise ValueError("Questionnaire items require an id and text")
        if self.facet is not None and not self.facet:
            raise ValueError("Questionnaire item facets cannot be empty")


@dataclass(frozen=True, slots=True)
class Questionnaire:
    id: str
    version: str
    title: str
    items: tuple[Item, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.version or not self.title:
            raise ValueError("Questionnaire metadata cannot be empty")
        if not self.items:
            raise ValueError("Questionnaire must contain at least one item")
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Questionnaire item ids must be unique")
