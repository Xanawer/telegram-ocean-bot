import hashlib
from collections import Counter
from pathlib import Path

import pytest

from ocean_bot.domain.catalog import load_bundled_questionnaires
from ocean_bot.domain.questionnaire import Trait
from ocean_bot.domain.scoring import score_questionnaire


def test_bundled_questionnaires_match_published_item_counts_and_keys() -> None:
    catalog = load_bundled_questionnaires()
    quick = catalog["mini-ipip-20"]
    detailed = catalog["ipip-50"]
    facet = catalog["ipip-120"]

    assert len(quick.items) == 20
    assert Counter(item.trait for item in quick.items) == {trait: 4 for trait in Trait}
    assert quick.items[0].text == "Am the life of the party."
    assert quick.items[5].reverse is True
    assert quick.items[8].trait is Trait.NEUROTICISM
    assert quick.items[8].reverse is True

    assert len(detailed.items) == 50
    assert Counter(item.trait for item in detailed.items) == {trait: 10 for trait in Trait}
    assert detailed.items[3].text == "Get stressed out easily."
    assert detailed.items[3].trait is Trait.NEUROTICISM
    assert detailed.items[3].reverse is False
    assert detailed.items[8].text == "Am relaxed most of the time."
    assert detailed.items[8].reverse is True

    assert len(facet.items) == 120
    assert Counter(item.trait for item in facet.items) == {trait: 24 for trait in Trait}
    assert set(Counter(item.facet for item in facet.items).values()) == {4}
    assert len({item.facet for item in facet.items}) == 30
    assert facet.items[0].text == "Worry about things."
    assert facet.items[0].facet == "Anxiety"
    assert facet.items[8].text == "Use others for my own ends."
    assert facet.items[8].reverse is True
    assert facet.items[115].text == "Remain calm under pressure."
    assert facet.items[115].trait is Trait.NEUROTICISM
    assert facet.items[115].reverse is True


@pytest.mark.parametrize(
    ("filename", "expected_sha256"),
    [
        (
            "mini_ipip_20.json",
            "71b279fd45a70dd398dc65f37d54bfcd3012f7ecb1d8c2396c544234d9958d88",
        ),
        (
            "ipip_50.json",
            "2eb6bc85e59d991fa3703856210fb0214c15f98276eb50948de387d0969215e1",
        ),
        (
            "ipip_120.json",
            "a38df4704595c05f6ae19e32e92e8fbec4a2168312cc7e029b55ad9241d07552",
        ),
    ],
)
def test_instrument_wording_order_and_keys_match_verified_snapshots(
    filename: str,
    expected_sha256: str,
) -> None:
    path = Path(__file__).parents[1] / "src" / "ocean_bot" / "data" / filename
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256


def test_all_published_keys_produce_fixed_worked_scores() -> None:
    catalog = load_bundled_questionnaires()
    expected = {
        "mini-ipip-20": {
            Trait.EXTRAVERSION: 3.0,
            Trait.AGREEABLENESS: 3.0,
            Trait.CONSCIENTIOUSNESS: 3.0,
            Trait.NEUROTICISM: 3.0,
            Trait.OPENNESS: 2.0,
        },
        "ipip-50": {
            Trait.EXTRAVERSION: 3.0,
            Trait.AGREEABLENESS: 3.4,
            Trait.CONSCIENTIOUSNESS: 3.4,
            Trait.NEUROTICISM: 4.2,
            Trait.OPENNESS: 3.8,
        },
        "ipip-120": {
            Trait.NEUROTICISM: 3.8333333333333335,
            Trait.EXTRAVERSION: 4.0,
            Trait.OPENNESS: 3.0,
            Trait.AGREEABLENESS: 2.1666666666666665,
            Trait.CONSCIENTIOUSNESS: 2.8333333333333335,
        },
    }
    for questionnaire_id, questionnaire in catalog.items():
        answers = {item.id: 5 for item in questionnaire.items}
        assert score_questionnaire(questionnaire, answers).raw_scores == expected[questionnaire_id]
