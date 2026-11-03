import pytest

from saverun.scoring import (ScoringError, max_lower_bound, parse_json_object, score_output,
                             validate_expected, variant_score, wilson)


def test_wilson_perfect_score_is_checkable_by_hand():
    # k = n  ->  lower = n / (n + z²), upper = 1
    lo, hi = wilson(10, 10)
    assert lo == pytest.approx(10 / (10 + 1.96 ** 2))
    assert round(lo, 3) == 0.722 and hi == 1.0


def test_wilson_zero_score():
    # k = 0  ->  lower = 0, upper = z² / (n + z²)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(1.96 ** 2 / (10 + 1.96 ** 2))


def test_wilson_reference_value():
    lo, hi = wilson(8, 10)  # classic textbook case
    assert (round(lo, 3), round(hi, 3)) == (0.490, 0.943)


def test_wilson_edges():
    assert wilson(0, 0) == (0.0, 0.0)
    with pytest.raises(ValueError):
        wilson(11, 10)


def test_interval_narrows_with_more_cases_only():
    narrow = wilson(80, 100)
    wide = wilson(8, 10)
    assert narrow[1] - narrow[0] < wide[1] - wide[0]


def test_max_lower_bound():
    assert max_lower_bound(12) == pytest.approx(12 / (12 + 1.96 ** 2))
    assert max_lower_bound(12) > 0.75 > max_lower_bound(10)  # 0.75 needs at least 12 selection cases


def test_variant_score_uses_case_rates():
    score, lo, hi = variant_score([1, 1, 0.5, 0])  # runs_per_case > 1 gives fractional rates
    assert score == pytest.approx(0.625) and lo < score < hi
    assert variant_score([]) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize("output,ok", [
    ("facturation", True),
    ("  Facturation.  ", True),
    ("« facturation »", True),
    ("Je réfléchis... Réponse : facturation", True),
    ("Réponse : technique\nRéponse : facturation", True),   # last marker wins
    ("technique", False),
    ("c'est de la facturation", False),                      # exact means exact
])
def test_exact(output, ok):
    assert score_output(output, "facturation", "exact").correct is ok


def test_json_fields_all_or_nothing_with_detail():
    exp = '{"categorie": "facturation", "urgent": true}'
    good = score_output('```json\n{"categorie": "Facturation", "urgent": true}\n```', exp, "json_fields")
    assert good.correct and good.detail == {"categorie": True, "urgent": True}
    half = score_output('Voici : {"categorie": "facturation", "urgent": false}', exp, "json_fields")
    assert not half.correct and half.detail == {"categorie": True, "urgent": False}
    assert not score_output("pas du json", exp, "json_fields").correct


def test_json_fields_restricted_to_listed_fields():
    exp = '{"categorie": "facturation", "note": "libre"}'
    assert score_output('{"categorie": "facturation"}', exp, "json_fields", ["categorie"]).correct
    assert not score_output('{"categorie": "facturation"}', exp, "json_fields").correct


def test_parse_json_object():
    assert parse_json_object('texte {"a": 1} fin') == {"a": 1}
    assert parse_json_object("[1, 2]") is None


def test_validate_expected_catches_problems_before_any_call():
    validate_expected(["a", "b"], "exact")
    with pytest.raises(ScoringError):
        validate_expected(["a", " "], "exact")
    with pytest.raises(ScoringError):
        validate_expected(['{"a": 1}', "pas json"], "json_fields")
    with pytest.raises(ScoringError):
        validate_expected(['{"a": 1}'], "json_fields", ["b"])


# --- replies written in the reasoning format (after an output_format) ----------------------------------

@pytest.mark.parametrize("reply,expected_true", [
    ('Le ton est pressant : échéance à 17h, donc {"urgent": true} est probable.\nRéponse : {"urgent": true}', True),
    ('Pas de date, simple information.\nRéponse : {"urgent": false}', False),
    ('Réflexion.\nRéponse : ```json\n{"urgent": false}\n```', False),
    ('Réflexion.\nRéponse : {"urgent": true}\n', True),
])
def test_replies_in_the_reasoning_format_with_json_are_scored(reply, expected_true):
    exp = '{"urgent": true}' if expected_true else '{"urgent": false}'
    assert score_output(reply, exp, "json_fields", ["urgent"]).correct
    other = '{"urgent": false}' if expected_true else '{"urgent": true}'
    assert not score_output(reply, other, "json_fields", ["urgent"]).correct
