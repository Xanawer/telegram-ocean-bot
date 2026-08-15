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
    }
    for questionnaire_id, questionnaire in catalog.items():
        answers = {item.id: 5 for item in questionnaire.items}
        assert score_questionnaire(questionnaire, answers).raw_scores == expected[questionnaire_id]
