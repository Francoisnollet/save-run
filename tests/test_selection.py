from types import SimpleNamespace as NS

import pytest

from saverun.config import parse_config
from saverun.cost import ModelPrice, PriceTable
from saverun.runner import CallResult
from saverun.scoring import variant_score, wilson
from saverun.selection import (NON_TESTE, NOT_SEPARATED_ALL, NOT_SEPARATED_DIRECT, PLUS_CHERE, QUASI, RETENUE,
                               SANS_REPONSE, SOUS_SEUIL, Row, build_rows, min_cases_for, not_separated, reachability,
                               select, validate, validation_pairs, winner_confirmed)

CFG = parse_config({"intention": "x", "seuil_qualite": 0.75,
                    "models": [{"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10}]})
PRICES = PriceTable("EUR", {"m": ModelPrice(1.0, 5.0, "d", "s"), "n": ModelPrice(2.0, 10.0, "d", "s", free=True)})


def row(variant, cost, k, n, model="m", **over):
    """A row built from k successes out of n cases, priced at `cost` per 1 000 calls."""
    score, lo, hi = variant_score([1] * k + [0] * (n - k))
    fields = dict(model=model, variant=variant, description=variant, n_cases=n, n_planned=n, score=score, lo=lo,
                  hi=hi, avg_in=100, avg_out=5, cost_per_1000=cost, estimated=False, failed_share=0.0,
                  truncated_share=0.0)
    return Row(**{**fields, **over})


def by_status(sel):
    return {r.variant: r.status for r in sel.rows}


# --- selection ---------------------------------------------------------------------

def test_threshold_is_on_the_lower_bound_never_on_the_raw_score():
    lucky = row("V01", 0.10, 5, 5)        # 100 % raw, but only 5 cases: lower bound about 57 %
    solid = row("V02", 0.50, 90, 100)     # 90 % raw, lower bound about 83 %
    assert lucky.score > solid.score and lucky.lo < 0.75 < solid.lo
    sel = select([lucky, solid], 0.75)
    assert sel.winner.variant == "V02"
    assert by_status(sel) == {"V01": SOUS_SEUIL, "V02": RETENUE}


def test_cheapest_variant_that_demonstrates_the_threshold_wins():
    sel = select([row("V01", 0.30, 90, 100), row("V02", 0.20, 95, 100), row("V03", 0.10, 60, 100)], 0.75)
    assert sel.winner.variant == "V02" and by_status(sel)["V03"] == SOUS_SEUIL
    assert [r.variant for r in sel.rows] == ["V03", "V02", "V01"]  # sorted by cost


def test_boundary_the_lower_bound_may_equal_the_threshold():
    r = row("V01", 0.1, 90, 100)
    assert select([r], r.lo).winner is not None
    assert select([r], r.lo + 0.001).winner is None


def test_near_ties_within_ten_percent_of_the_winner_cost():
    rows = [row("V01", 0.100, 95, 100), row("V02", 0.109, 95, 100), row("V03", 0.111, 95, 100),
            row("V04", 0.105, 50, 100)]
    sel = select(rows, 0.75)
    assert by_status(sel) == {"V01": RETENUE, "V04": SOUS_SEUIL, "V02": QUASI, "V03": PLUS_CHERE}
    assert [r.variant for r in sel.near_ties] == ["V02"]


def test_a_near_tie_must_itself_demonstrate_the_threshold():
    sel = select([row("V01", 0.100, 95, 100), row("V02", 0.101, 70, 100)], 0.75)
    assert sel.near_ties == [] and by_status(sel)["V02"] == SOUS_SEUIL


def test_no_variant_demonstrates_the_threshold():
    sel = select([row("V01", 0.1, 8, 10), row("V02", 0.2, 10, 10)], 0.95)
    assert sel.winner is None and sel.near_ties == [] and validation_pairs(sel) == []
    assert set(by_status(sel).values()) == {SOUS_SEUIL}


def test_models_compete_on_cost_and_equal_cost_is_deterministic():
    rows = [row("V01", 0.20, 95, 100, model="n"), row("V02", 0.20, 95, 100, model="m"),
            row("V01", 0.50, 99, 100, model="m")]
    sel = select(rows, 0.75)
    assert (sel.winner.model, sel.winner.variant) == ("m", "V02")
    assert select(list(reversed(rows)), 0.75).winner == sel.winner


def test_untested_or_empty_rows_never_compete():
    rows = [row("V01", None, 0, 0, model="n", status=NON_TESTE, note="clé refusée"),
            row("V02", None, 0, 0, status=SANS_REPONSE), row("V03", 0.3, 95, 100)]
    sel = select(rows, 0.75)
    assert sel.winner.variant == "V03"
    assert by_status(sel)["V01"] == NON_TESTE and by_status(sel)["V02"] == SANS_REPONSE


# --- rows from raw results -----------------------------------------------------------

def res(variant, case, status="ok", out="a", tin=100, tout=10, model="m", phase="selection", truncated=False,
        error=None):
    return CallResult(model, variant, "h", case, "i", 0, phase, None, 300, status,
                      out if status == "ok" else None, tin if status == "ok" else None,
                      tout if status == "ok" else None, truncated, error)


class V:
    def __init__(self, id):
        self.id, self.description = id, f"variante {id}"


def test_build_rows_scores_cases_and_prices_from_reported_tokens():
    expected = {f"C{i}": "a" for i in range(10)}
    results = [res("V01", f"C{i}", out="a" if i < 8 else "b", tin=300, tout=10) for i in range(10)]
    (r,) = build_rows(results, expected, [V("V01")], CFG, PRICES)
    assert (r.n_cases, r.score) == (10, 0.8)
    assert (r.lo, r.hi) == pytest.approx(wilson(8, 10))
    assert (r.avg_in, r.avg_out) == (300, 10)
    assert r.cost_per_1000 == pytest.approx((300 * 1.0 + 10 * 5.0) / 1e6 * 1000)  # 0.35


def test_failed_calls_leave_the_score_and_the_token_means():
    expected = {f"C{i}": "a" for i in range(10)}
    results = ([res("V01", f"C{i}", tin=200) for i in range(8)]
               + [res("V01", "C8", "failed", error="e"), res("V01", "C9", "failed", error="e")])
    (r,) = build_rows(results, expected, [V("V01")], CFG, PRICES)
    assert r.n_cases == 8 and r.score == 1.0         # not 8/10: failures are not wrong answers
    assert r.avg_in == 200 and r.failed_share == pytest.approx(0.2) and r.unfair


def test_truncated_share_and_free_model_flag():
    results = [res("V01", f"C{i}", truncated=i < 3, model="n") for i in range(10)]
    (r,) = build_rows(results, {f"C{i}": "a" for i in range(10)}, [V("V01")], CFG, PRICES)
    assert r.truncated_share == pytest.approx(0.3) and r.estimated


def test_wrong_answers_count_those_truncated_and_the_score_does_not_change():
    expected = {f"C{i}": "a" for i in range(5)}
    results = [res("V01", "C0"), res("V01", "C1", out="b"), res("V01", "C2", out="b", truncated=True),
               res("V01", "C3", truncated=True), res("V01", "C4", status="failed", out=None)]
    (r,) = build_rows(results, expected, [V("V01")], CFG, PRICES)
    assert (r.wrong_answers, r.wrong_truncated) == (2, 1)
    assert r.score == 0.5 and r.n_cases == 4 and r.truncated_share == pytest.approx(0.5)


def test_off_format_share_counts_unreadable_answers_truncated_apart():
    expected = {f"C{i}": "a" for i in range(6)}
    results = [res("V01", "C0", out="a"), res("V01", "C1", out="b"),             # right, wrong but readable
               res("V01", "C2", out="c'est a"), res("V01", "C3", out="zzz"),     # off format
               res("V01", "C4", out="zz", truncated=True),                        # truncated: counted apart
               res("V01", "C5", status="failed", out=None)]                       # failed: not an answer
    (r,) = build_rows(results, expected, [V("V01")], CFG, PRICES, labels=frozenset({"a", "b"}))
    assert r.off_format_share == pytest.approx(2 / 5) and r.truncated_share == pytest.approx(1 / 5)
    assert r.score == pytest.approx(1 / 5)                                        # the score does not change
    (unknown,) = build_rows(results, expected, [V("V01")], CFG, PRICES)            # exact without a closed list
    assert unknown.off_format_share is None


def test_off_format_share_with_json_fields():
    cfg = parse_config({"intention": "x", "scoring": "json_fields", "fields": ["urgent"],
                        "models": [{"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10}]})
    expected = {f"C{i}": '{"urgent": true}' for i in range(4)}
    results = [res("V01", "C0", out='{"urgent": true}'), res("V01", "C1", out='Answer: {"urgent": false}'),
               res("V01", "C2", out="Urgent."), res("V01", "C3", out='{"urg', truncated=True)]
    (r,) = build_rows(results, expected, [V("V01")], cfg, PRICES)
    assert r.off_format_share == pytest.approx(1 / 4)


def test_all_failed_and_untested_models():
    expected = {"C0": "a", "C1": "a"}
    results = ([res("V01", "C0", "failed", error="e"), res("V01", "C1", "failed", error="e")]
               + [res("V01", "C0", "skipped", error="clé refusée ; modèle arrêté", model="n"),
                  res("V01", "C1", "skipped", error="clé refusée ; modèle arrêté", model="n")])
    rows = {r.model: r for r in build_rows(results, expected, [V("V01")], CFG, PRICES)}
    assert rows["m"].status == SANS_REPONSE and rows["m"].cost_per_1000 is None
    assert rows["n"].status == NON_TESTE and "clé refusée" in rows["n"].note


# --- validation ----------------------------------------------------------------------

def sel_with_tie():
    return select([row("V01", 0.100, 95, 100), row("V02", 0.105, 95, 100), row("V03", 0.9, 99, 100)], 0.75)


def val_results(variant, n_right, n, model="m"):
    return [res(variant, f"V{i}", out="a" if i < n_right else "b", model=model, phase="validation")
            for i in range(n)]


def test_validation_pairs_are_winner_then_near_ties():
    assert validation_pairs(sel_with_tie()) == [("m", "V01"), ("m", "V02")]


def test_confirmation_uses_the_raw_score_and_shows_the_interval():
    expected = {f"V{i}": "a" for i in range(4)}
    v = validate(val_results("V01", 3, 4) + val_results("V02", 4, 4), expected, sel_with_tie(), CFG)
    first = v[0]
    assert first.is_winner and first.confirmed and first.score == 0.75
    assert first.lo < 0.75                                  # the lower bound is far lower, and is shown
    assert (first.lo, first.hi) == pytest.approx(wilson(3, 4))
    assert first.indicative                                 # 4 cases: indicative only


def test_unconfirmed_winner_is_not_replaced_by_a_near_tie():
    expected = {f"V{i}": "a" for i in range(10)}
    sel = sel_with_tie()
    v = validate(val_results("V01", 5, 10) + val_results("V02", 10, 10), expected, sel, CFG)
    assert winner_confirmed(v) is False
    assert [x.confirmed for x in v] == [False, True]        # the near-tie is shown, confirmed...
    assert sel.winner.variant == "V01"                      # ...but the recommendation does not move
    assert sum(x.is_winner for x in v) == 1


def test_indicative_below_twenty_cases():
    expected = {f"V{i}": "a" for i in range(20)}
    sel = select([row("V01", 0.1, 95, 100)], 0.75)
    assert validate(val_results("V01", 20, 20), expected, sel, CFG)[0].indicative is False
    expected = {f"V{i}": "a" for i in range(19)}
    assert validate(val_results("V01", 19, 19), expected, sel, CFG)[0].indicative is True


def test_validation_without_usable_answers_is_not_confirmed():
    sel = select([row("V01", 0.1, 95, 100)], 0.75)
    failed = [res("V01", f"V{i}", "failed", phase="validation", error="e") for i in range(5)]
    (v,) = validate(failed, {f"V{i}": "a" for i in range(5)}, sel, CFG)
    assert v.n_cases == 0 and v.confirmed is False


def test_no_winner_means_nothing_to_validate():
    sel = select([row("V01", 0.1, 3, 10)], 0.75)
    assert validate([], {}, sel, CFG) == [] and winner_confirmed([]) is None


# --- reachability ----------------------------------------------------------------------

def test_reachability_message():
    msg, ok = reachability(13, 0.75)
    assert ok and "13 cas de sélection" in msg and "77,2 %" in msg
    msg, ok = reachability(10, 0.75)
    assert not ok and "72,2 %" in msg and "inatteignable" in msg and "12 cas" in msg


def test_min_cases_for():
    assert min_cases_for(0.75) == 12 and min_cases_for(0.90) == 35
    assert wilson(12, 12)[0] >= 0.75 > wilson(11, 11)[0]


# --- does the test set separate the variants? --------------------------------------------------

def variants(*with_reasoning, all_ids=("V01", "V02", "V03", "V04", "V05")):
    return [NS(id=i, reasoning=i in with_reasoning) for i in all_ids]


def scored(*scores, n=100, model="m"):
    return [row(f"V{i:02d}", 1.0, s, n, model=model) for i, s in enumerate(scores, start=1)]


def test_rule_a_at_exactly_80_percent_of_the_variants_at_98_percent():
    rows = scored(98, 98, 98, 98, 50)
    assert not_separated(rows, variants("V05"), 0.7) == [f"m : {NOT_SEPARATED_ALL}"]
    assert not_separated(scored(98, 98, 98, 90, 50), variants("V05"), 0.7) == []   # 3 out of 5, gap of 8 points


def test_rule_b_at_exactly_three_points_among_the_variants_without_reasoning():
    rows = scored(95, 92, 94, 10, 10)
    assert not_separated(rows, variants("V04", "V05"), 0.7) == [f"m : {NOT_SEPARATED_DIRECT}"]
    assert not_separated(scored(95, 91, 94, 10, 10), variants("V04", "V05"), 0.7) == []


def test_rule_b_tolerates_one_case_when_a_case_weighs_more_than_three_points():
    rows = scored(20, 19, 20, n=20)
    ids = ("V01", "V02", "V03")
    assert not_separated(rows, variants(all_ids=ids), 0.7) == [f"m : {NOT_SEPARATED_DIRECT}"]
    assert not_separated(scored(20, 18, 20, n=20), variants(all_ids=ids), 0.7) == []


def test_rule_b_needs_a_variant_at_the_threshold():
    ids = ("V01", "V02", "V03")
    assert not_separated(scored(55, 54, 55), variants(all_ids=ids), 0.7) == []
    assert not_separated(scored(70, 69, 70), variants(all_ids=ids), 0.7) == [f"m : {NOT_SEPARATED_DIRECT}"]


def test_too_few_variants_to_compare():
    ids = ("V01", "V02")
    assert not_separated(scored(100, 100), variants(all_ids=ids), 0.7) == []
    # Rule B needs three variants without reasoning: two here, far from the ones with reasoning.
    four = ("V01", "V02", "V03", "V04")
    assert not_separated(scored(90, 90, 10, 10), variants("V03", "V04", all_ids=four), 0.7) == []


def test_models_are_judged_apart_and_named_together():
    rows = scored(100, 100, 100, model="a") + scored(100, 100, 100, model="b") + scored(100, 50, 20, model="c")
    ids = ("V01", "V02", "V03")
    assert not_separated(rows, variants(all_ids=ids), 0.7) == [f"a, b : {NOT_SEPARATED_ALL}"]


def test_rule_a_comes_first_for_a_model_and_each_message_is_listed():
    rows = scored(100, 100, 100, model="a") + scored(90, 90, 90, 10, model="b")
    ids = ("V01", "V02", "V03", "V04")
    assert not_separated(rows, variants("V04", all_ids=ids), 0.7) == [f"a : {NOT_SEPARATED_ALL}",
                                                                      f"b : {NOT_SEPARATED_DIRECT}"]


def test_rows_not_comparable_are_left_out():
    rows = [*scored(100, 100, 100), row("V04", 1.0, 10, 100, failed_share=0.5),
            row("V05", 1.0, 0, 100, n_cases=0, status=NON_TESTE)]
    assert not_separated(rows, variants(), 0.7) == [f"m : {NOT_SEPARATED_ALL}"]
    assert not_separated([row("V01", None, 0, 100, n_cases=0, status=NON_TESTE)], variants(), 0.7) == []


def test_base_counts_as_a_variant_without_reasoning():
    rows = [row("BASE", 1.0, 95, 100), *scored(95, 95), row("V03", 1.0, 10, 100)]
    vs = [NS(id="BASE", reasoning=None), *variants("V03", all_ids=("V01", "V02", "V03"))]
    assert not_separated(rows, vs, 0.7) == [f"m : {NOT_SEPARATED_DIRECT}"]
