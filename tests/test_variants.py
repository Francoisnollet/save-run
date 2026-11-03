import pytest

from saverun.config import parse_config
from saverun.fake import EXAMPLES_CUE, FINAL_MARKER, PERSONA_CUE, REASONING_CUE
from saverun.runner import build_jobs
from saverun.scoring import ANSWER_MARKER
from saverun.splitting import Case
from saverun.variants import BASE_ID, REASONING, REASONING_WITH_FORMAT, base_variant, build_variants

MODEL = {"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10}
EXAMPLES = [Case(f"C{i}", f"ticket {i}", f"cat{i}") for i in range(1, 4)]


def cfg(**over):
    return parse_config({"intention": "Classe le ticket.", "models": [MODEL], "role": "un agent expérimenté", **over})


def test_ids_are_stable_and_the_plainest_variant_is_first():
    variants, warnings = build_variants(cfg(), EXAMPLES)
    assert [v.id for v in variants] == [f"V{i:02d}" for i in range(1, 21)] and not warnings
    v1 = variants[0]
    assert (v1.persona, v1.shots, v1.reasoning, v1.concise) == ("aucun", 0, False, True)
    assert v1.system == "Classe le ticket.\n\nRéponds uniquement par la réponse attendue, sans explication."
    again, _ = build_variants(cfg(), EXAMPLES)
    assert [v.system for v in again] == [v.system for v in variants]


def test_seed_changes_the_draw_but_not_v01():
    a, _ = build_variants(cfg(), EXAMPLES, n=8, seed=1)
    b, _ = build_variants(cfg(), EXAMPLES, n=8, seed=2)
    assert a[0].system == b[0].system and [v.system for v in a] != [v.system for v in b]


def test_variants_are_all_different_with_readable_descriptions():
    variants, _ = build_variants(cfg(), EXAMPLES, n=24)
    assert len({v.system for v in variants}) == 24 == len({v.description for v in variants})
    assert "persona=métier, exemples=3, raisonnement=oui, condensé=non" in {v.description for v in variants}


def test_levers_show_up_in_the_prompt():
    variants, _ = build_variants(cfg(), EXAMPLES, n=24)
    for v in variants:
        assert ("Tu es un agent expérimenté." in v.system) == (v.persona == "métier")
        assert ("Lis attentivement" in v.system) == (not v.concise)
        assert (v.shots == 3) == ("Entrée : ticket 1\nSortie : cat1" in v.system)
        assert ("étape par étape" in v.system) == v.reasoning
    three = next(v for v in variants if v.shots == 3)
    assert all(f"ticket {i}" in three.system and f"cat{i}" in three.system for i in (1, 2, 3))


def test_without_role_there_is_no_domain_persona():
    variants, warnings = build_variants(cfg(role=None), EXAMPLES, n=20)
    assert len(variants) == 16 and all(v.persona != "métier" for v in variants)
    assert warnings and "role" in warnings[0] and "16" in warnings[0]


def test_json_scoring_asks_for_json():
    variants, _ = build_variants(cfg(scoring="json_fields"), EXAMPLES, n=4)
    assert "JSON" in variants[0].system


def test_fake_model_cues_match_what_the_templates_produce():
    """The fake model reacts to textual cues; a template change must not silently deafen it."""
    variants, _ = build_variants(cfg(), EXAMPLES, n=24)
    for v in variants:
        assert (EXAMPLES_CUE in v.system) == (v.shots == 3)
        assert (REASONING_CUE in v.system) == v.reasoning
        assert (PERSONA_CUE in v.system) == (v.persona != "aucun")
    assert FINAL_MARKER == ANSWER_MARKER


def test_base_variant():
    assert base_variant(cfg()) is None
    b = base_variant(cfg(base_prompt="Mon prompt actuel."))
    assert b.id == BASE_ID and b.system == "Mon prompt actuel." and b.is_base


def test_needs_three_examples():
    with pytest.raises(ValueError):
        build_variants(cfg(), EXAMPLES[:2])


def test_jobs_cover_variants_models_cases_and_runs():
    variants, _ = build_variants(cfg(runs_per_case=2), EXAMPLES, n=5)
    cases = [Case(f"C{i}", f"in{i}", "x") for i in range(1, 7)]
    jobs = build_jobs(variants + [base_variant(cfg(base_prompt="p"))], cfg(runs_per_case=2), cases, "selection")
    assert len(jobs) == 6 * 1 * 6 * 2 and len({j.key for j in jobs}) == len(jobs)


# --- output_format: how the model must answer, in every generated variant ------------------------------

FORMAT = 'Réponds avec le JSON brut uniquement, sans balises markdown : {"urgent": true ou false}'


def test_output_format_is_in_every_variant_including_the_one_without_examples():
    variants, _ = build_variants(cfg(scoring="json_fields", output_format=FORMAT), EXAMPLES, n=24)
    assert all(FORMAT in v.system for v in variants)
    v01 = variants[0]
    assert (v01.shots, v01.reasoning) == (0, False)
    assert v01.system == f"Classe le ticket.\n\n{FORMAT}"          # intention, then the format: nothing else


def test_without_reasoning_the_format_replaces_the_generic_format_line():
    variants, _ = build_variants(cfg(scoring="json_fields", output_format=FORMAT), EXAMPLES, n=24)
    for v in (v for v in variants if not v.reasoning):
        assert v.system.endswith(FORMAT) and "objet JSON valide" not in v.system
        assert v.system.count(FORMAT) == 1


def test_with_reasoning_the_format_is_scoped_to_what_follows_the_marker():
    variants, _ = build_variants(cfg(scoring="json_fields", output_format=FORMAT), EXAMPLES, n=24)
    reasoning = [v for v in variants if v.reasoning]
    assert reasoning
    for v in reasoning:
        assert v.system.count(FORMAT) == 1
        assert v.system.index(FORMAT) < v.system.index("Réfléchis d'abord étape par étape")   # format, then how to think
        assert v.system.endswith(REASONING_WITH_FORMAT)
        assert f"Ce format ne s'applique qu'à ce qui suit « {ANSWER_MARKER} »" in v.system     # no clash with "sans texte avant ou après"
        assert "ton raisonnement reste libre" in v.system and "au format demandé ci-dessus" in v.system


def test_reasoning_without_output_format_keeps_its_original_wording():
    variants, _ = build_variants(cfg(), EXAMPLES, n=24)
    assert all(v.system.endswith(REASONING) for v in variants if v.reasoning)


def test_examples_come_before_the_format_so_the_format_stays_last():
    variants, _ = build_variants(cfg(output_format=FORMAT), EXAMPLES, n=24)
    shots = [v for v in variants if v.shots == 3 and not v.reasoning]
    assert shots and all(v.system.index("Exemples :") < v.system.index(FORMAT) for v in shots)


def test_without_output_format_prompts_are_unchanged():
    variants, _ = build_variants(cfg(), EXAMPLES, n=1)
    assert variants[0].system == "Classe le ticket.\n\nRéponds uniquement par la réponse attendue, sans explication."


def test_fake_model_cues_still_match_with_an_output_format():
    variants, _ = build_variants(cfg(output_format=FORMAT), EXAMPLES, n=24)
    for v in variants:
        assert (EXAMPLES_CUE in v.system) == (v.shots == 3)
        assert (REASONING_CUE in v.system) == v.reasoning
        assert (PERSONA_CUE in v.system) == (v.persona != "aucun")
