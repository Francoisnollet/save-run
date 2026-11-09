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


# --- expected_label, and replies written in the scoped reasoning format ------------------------------------------

from saverun.scoring import expected_label


def test_expected_label_is_what_scoring_compares():
    assert expected_label("Facturation.", "exact") == expected_label(" facturation ", "exact")
    assert expected_label("a", "exact") != expected_label("b", "exact")
    one = expected_label('{"urgent": true, "note": "x"}', "json_fields", ["urgent"])
    assert one == expected_label('{"note": "y", "urgent": true}', "json_fields", ["urgent"])
    assert one != expected_label('{"urgent": false, "note": "x"}', "json_fields", ["urgent"])
    assert expected_label('{"b": 1, "a": "Oui."}', "json_fields") == expected_label('{"a": "oui", "b": 1}', "json_fields")


from saverun.scoring import ANSWER_MARKER, extract_answer


def test_answer_marker_in_prompts_is_unchanged():
    # ANSWER_MARKER is written into the reasoning prompts: changing it changes every prompt hash, hence the cache key,
    # and every stored call would be paid again. The tolerant reading lives in a separate regex.
    assert ANSWER_MARKER == "Réponse :"


@pytest.mark.parametrize("reply", [
    "Réflexion.\nRéponse : pending_transfer",
    "Réflexion.\nRéponse: pending_transfer",
    "Réflexion.\nRéponse\u00a0: pending_transfer",              # no-break space
    "Réflexion.\nRéponse\u202f: pending_transfer",              # narrow no-break space
    "Réflexion.\nRÉPONSE : pending_transfer",
    "Let me think.\nAnswer: pending_transfer",
    "Let me think.\nanswer : pending_transfer",
    "Réflexion.\n**Réponse :** pending_transfer",
    "Réflexion.\n**Réponse : pending_transfer**",
    "Réflexion.\nRéponse : **pending_transfer**",
    "Let me think.\n**Answer:** pending_transfer",
    "Let me think.\n### Answer: pending_transfer",
    "Let me think.\n> Answer: pending_transfer",
    "  Réponse: pending_transfer",
    "Réflexion... Réponse : pending_transfer",                  # the exact marker is still accepted mid-line
])
def test_tolerant_marker_forms(reply):
    assert extract_answer(reply) == "pending_transfer"
    assert score_output(reply, "pending_transfer", "exact").correct
    assert not score_output(reply, "failed_transfer", "exact").correct


def test_last_marker_wins_across_forms():
    assert extract_answer("Answer: failed_transfer, maybe.\nOn second thought...\nRéponse: pending_transfer") \
        == "pending_transfer"
    assert extract_answer("Réponse : failed_transfer\nAnswer: pending_transfer") == "pending_transfer"
    assert extract_answer("**Réponse :** a\n\n**Answer:** b") == "b"


@pytest.mark.parametrize("reply", [
    "I think my answer: pending_transfer is likely, but no.\nfailed_transfer",   # mid-line, lower case
    "Réponses : pending_transfer",                                               # plural
    "The customer answered: pending_transfer",
    "Answer pending_transfer",                                                   # no colon
    "La réponse : pending_transfer",                                             # lower case mid-line
    "Final answer: pending_transfer",                                            # not in the accepted list
])
def test_no_false_marker(reply):
    assert extract_answer(reply) == reply
    assert not score_output(reply, "pending_transfer", "exact").correct


def test_without_marker_nothing_changes_bold_included():
    for reply in ["pending_transfer", "  Pending_transfer. ", "The closest category is **pending_transfer**.",
                  "**pending_transfer**", '{"urgent": true}']:
        assert extract_answer(reply) == reply
    assert score_output("pending_transfer", "pending_transfer", "exact").correct
    assert not score_output("**pending_transfer**", "pending_transfer", "exact").correct
    assert not score_output("The closest category is **pending_transfer**.", "pending_transfer", "exact").correct


@pytest.mark.parametrize("reply", [
    'Réflexion.\nRéponse: {"urgent": true}',
    'Let me think.\n**Answer:** ```json\n{"urgent": true}\n```',
    'Réflexion.\n**Réponse :**\n\n{"urgent": true}',
    'Exemple de format : {"urgent": false}. Ici l\'échéance est à 17h.\nAnswer: {"urgent": true}',
])
def test_tolerant_marker_with_json(reply):
    assert score_output(reply, '{"urgent": true}', "json_fields", ["urgent"]).correct
    assert not score_output(reply, '{"urgent": false}', "json_fields", ["urgent"]).correct


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
