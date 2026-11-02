import pytest

from saverun.config import parse_config
from saverun.cost import ModelPrice, PriceTable
from saverun.runner import CallResult
from saverun.scoring import variant_score, wilson
from saverun.selection import (NON_TESTE, PLUS_CHERE, QUASI, RETENUE, SANS_REPONSE, SOUS_SEUIL, Row, build_rows,
                               min_cases_for, reachability, select, validate, validation_pairs, winner_confirmed)

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
