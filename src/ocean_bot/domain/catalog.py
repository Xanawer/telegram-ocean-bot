from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from typing import Any

from ocean_bot.domain.questionnaire import Item, Questionnaire, Trait

_EXPECTED_COUNTS = {"mini-ipip-20": 20, "ipip-50": 50, "ipip-120": 120}
_EXPECTED_SHA256 = {
    "mini_ipip_20.json": "71b279fd45a70dd398dc65f37d54bfcd3012f7ecb1d8c2396c544234d9958d88",
    "ipip_50.json": "2eb6bc85e59d991fa3703856210fb0214c15f98276eb50948de387d0969215e1",
    "ipip_120.json": "a38df4704595c05f6ae19e32e92e8fbec4a2168312cc7e029b55ad9241d07552",
}


def load_bundled_questionnaires() -> dict[str, Questionnaire]:
    data_root = files("ocean_bot").joinpath("data")
    questionnaires = {}
    for filename in ("mini_ipip_20.json", "ipip_50.json", "ipip_120.json"):
        raw = data_root.joinpath(filename).read_bytes()
        if hashlib.sha256(raw).hexdigest() != _EXPECTED_SHA256[filename]:
            raise ValueError(f"Questionnaire checksum mismatch for {filename}")
        document = json.loads(raw)
        questionnaire = _parse_questionnaire(document)
        questionnaires[questionnaire.id] = questionnaire
    return questionnaires


def _parse_questionnaire(document: Any) -> Questionnaire:
    if not isinstance(document, dict):
        raise ValueError("Questionnaire document must be an object")
    try:
        questionnaire_id = str(document["id"])
        raw_items = document["items"]
        if not isinstance(raw_items, list):
            raise ValueError("Questionnaire items must be a list")
        items = tuple(
            Item(
                id=str(raw["id"]),
                text=str(raw["text"]),
                trait=Trait(raw["trait"]),
                reverse=raw["reverse"],
                facet=str(raw["facet"]) if "facet" in raw else None,
            )
            for raw in raw_items
        )
        questionnaire = Questionnaire(
            id=questionnaire_id,
            version=str(document["version"]),
            title=str(document["title"]),
            items=items,
        )
    except (KeyError, TypeError) as error:
        raise ValueError("Malformed questionnaire document") from error

    expected_count = _EXPECTED_COUNTS.get(questionnaire.id)
    if expected_count is None or len(questionnaire.items) != expected_count:
        raise ValueError(f"Unexpected item count for {questionnaire.id}")
    trait_counts = {trait: 0 for trait in Trait}
    for item in questionnaire.items:
        trait_counts[item.trait] += 1
    if len(set(trait_counts.values())) != 1 or 0 in trait_counts.values():
        raise ValueError(f"Unbalanced trait coverage for {questionnaire.id}")
    if questionnaire.id == "ipip-120":
        facets = [item.facet for item in questionnaire.items]
        if any(facet is None for facet in facets):
            raise ValueError("IPIP-NEO-120 items require facet labels")
        facet_counts = {facet: facets.count(facet) for facet in set(facets)}
        if len(facet_counts) != 30 or set(facet_counts.values()) != {4}:
            raise ValueError("IPIP-NEO-120 requires four items for each of 30 facets")
    return questionnaire
