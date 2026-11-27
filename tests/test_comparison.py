"""Case-by-case comparison with the current prompt. Fake results and the fake model only: no network, no key."""
import dataclasses

import pytest

from saverun.comparison import (Comparison, at_threshold, compare, compare_rows, critical_passed, passed,
                                rates_by_couple, reference_pair)
from saverun.config import load_config, parse_config
from saverun.cost import ModelPrice, PriceTable, load_prices
from saverun.fake import FakeClient
from saverun.pipeline import execute_plan, prepare
from saverun.runner import CallResult, ModelRuntime, RunStore
from saverun.selection import NON_TESTE, SANS_REPONSE, build_rows, select
from saverun.splitting import Case, load_cases

MODELS = [{"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10},
          {"id": "n", "provider": "anthropic", "api_key_env": "K", "rpm": 10}]
PRICES = PriceTable("EUR", {"m": ModelPrice(1.0, 5.0, "d", "s"), "n": ModelPrice(0.5, 2.0, "d", "s")})


def cfg(**over):
    return parse_config({"intention": "x", "models": MODELS, "base_prompt": "actuel", "current_model": "m", **over})


def cases(n, critical=()):
    return [Case(f"C{i}", f"in{i}", "a", critical=i in critical) for i in range(n)]


def res(variant, case, out="a", model="m", status="ok", run=0, phase="selection"):
    return CallResult(model, variant, "h", case, "i", run, phase, None, 300, status,
                      out if status == "ok" else None, 10 if status == "ok" else None,
                      2 if status == "ok" else None, False, None if status == "ok" else "e")


def answers(variant, pattern, model="m"):
    """One result per case: 'o' right, 'x' wrong, 'f' failed call."""
    return [res(variant, f"C{i}", out="a" if p == "o" else "b", model=model, status="failed" if p == "f" else "ok")
            for i, p in enumerate(pattern)]


class V:
    def __init__(self, id):
        self.id, self.description = id, id


def rows_for(results, config, the_cases):
    expected = {c.id: c.expected for c in the_cases}
    ids = sorted({r.variant for r in results})
    return build_rows(results, expected, [V(i) for i in ids], config, PRICES)


# --- the three sets ------------------------------------------------------------------------------

def test_regressions_corrections_and_common_failures():
    ref = {"C0": 1, "C1": 1, "C2": 0, "C3": 0, "C4": 1}
    var = {"C0": 1, "C1": 0, "C2": 1, "C3": 0, "C4": 0}
    c = compare(ref, var, cases(5, critical={1}), 0.6)
    assert c == Comparison(regressions=("C1", "C4"), corrections=("C2",), common_failures=("C3",),
                           critical_regressions=("C1",))


def test_a_case_without_a_completed_run_on_either_side_is_unverified():
    ref = {"C0": 1, "C1": 1}
    var = {"C0": 0, "C2": 1}
    c = compare(ref, var, cases(3, critical={0, 2}), 0.6)
    assert c.unverified == ("C1", "C2") and c.critical_unverified == ("C2",)
    assert c.regressions == ("C0",) and c.critical_regressions == ("C0",)   # a failed call is never a regression


def test_success_rate_decides_what_passed_means():
    assert passed(0.6, 0.6) and passed(3 / 5, 0.6) and not passed(0.59, 0.6)
    ref, var = {"C0": 1.0}, {"C0": 2 / 3}
    assert compare(ref, var, cases(1), 0.6).regressions == ()
    assert compare(ref, var, cases(1), 0.7).regressions == ("C0",)
    assert compare(ref, var, cases(1), 1.0).regressions == ("C0",)


@pytest.mark.parametrize("success_rate", [0.01, 0.5, 0.6, 1.0])
def test_with_one_run_success_rate_changes_nothing(success_rate):
    ref, var = {"C0": 1, "C1": 0, "C2": 1}, {"C0": 0, "C1": 1, "C2": 1}
    c = compare(ref, var, cases(3), success_rate)
    assert (c.regressions, c.corrections, c.common_failures) == (("C0",), ("C1",), ())


def test_critical_cases_passed_among_those_with_a_completed_run():
    rates = {"C0": 1, "C1": 0, "C2": 2 / 3}            # C3 critical but never answered
    assert critical_passed(rates, cases(4, critical={0, 1, 2, 3}), 0.6) == (2, 3)
    assert critical_passed(rates, cases(4), 0.6) == (0, 0)


def test_cases_exactly_at_success_rate_are_listed():
    rates = {("m", "V01"): {"C0": 3 / 5, "C1": 1.0}, ("n", "BASE"): {"C2": 0.6, "C3": 0.4}}
    assert at_threshold(rates, 0.6) == [("m", "V01", "C0"), ("n", "BASE", "C2")]


def test_rates_by_couple_keeps_one_phase_and_averages_the_runs():
    results = [res("V01", "C0", run=0), res("V01", "C0", out="b", run=1), res("V01", "C1", phase="validation")]
    assert rates_by_couple(results, {"C0": "a", "C1": "a"}, cfg(), "selection") == {("m", "V01"): {"C0": 0.5}}


# --- the reference -----------------------------------------------------------------------------

def test_the_reference_is_base_on_current_model():
    assert reference_pair(cfg()) == (("m", "BASE"), None)


@pytest.mark.parametrize("over,fragment", [({"base_prompt": None}, "base_prompt"),
                                           ({"current_model": None}, "current_model")])
def test_no_reference_no_comparison_and_the_reason_is_given(over, fragment):
    config = cfg(**over)
    the_cases = cases(3, critical={0})
    results = answers("BASE", "ooo") + answers("V01", "oxx")
    rows, ctx = compare_rows(rows_for(results, config, the_cases), results, the_cases, config)
    assert ctx.reference is None and fragment in ctx.reason
    assert all(r.comparison is None for r in rows)
    assert {r.variant: (r.critical_passed, r.critical_n) for r in rows} == {"BASE": (1, 1), "V01": (1, 1)}


def test_base_of_another_model_is_compared_with_the_current_model():
    config = cfg()
    the_cases = cases(4)
    results = answers("BASE", "ooxx") + answers("BASE", "oxox", model="n") + answers("V01", "xoox", model="n")
    rows, ctx = compare_rows(rows_for(results, config, the_cases), results, the_cases, config)
    by = {(r.model, r.variant): r.comparison for r in rows}
    assert ctx.reference == ("m", "BASE") and ctx.reason is None
    assert by[("m", "BASE")] == Comparison(common_failures=("C2", "C3"))       # the reference against itself
    assert by[("n", "BASE")] == Comparison(regressions=("C1",), corrections=("C2",), common_failures=("C3",))
    assert by[("n", "V01")] == Comparison(regressions=("C0",), corrections=("C2",), common_failures=("C3",))


def test_an_unmeasured_reference_gives_no_comparison():
    config = cfg()
    the_cases = cases(2)
    results = answers("BASE", "ff") + answers("V01", "oo")
    rows, ctx = compare_rows(rows_for(results, config, the_cases), results, the_cases, config)
    assert ctx.reference is None and "n'a pas pu être mesuré sur m" in ctx.reason
    assert all(r.comparison is None for r in rows)


def test_untested_rows_get_no_comparison():
    config = cfg()
    the_cases = cases(2)
    results = answers("BASE", "oo") + [res("V01", f"C{i}", model="n", status="skipped") for i in range(2)]
    results = [dataclasses.replace(r, error="clé refusée ; modèle arrêté") if r.status == "skipped" else r
               for r in results]
    rows, _ = compare_rows(rows_for(results, config, the_cases), results, the_cases, config)
    untested = next(r for r in rows if r.model == "n")
    assert untested.status == NON_TESTE and untested.comparison is None and untested.critical_n == 0


def test_at_threshold_is_only_reported_with_several_runs():
    the_cases = cases(1)
    results = [res("V01", "C0", out="a" if run < 3 else "b", run=run) for run in range(5)]
    results += [res("BASE", "C0", run=run) for run in range(5)]
    for runs, expected in [(5, [("m", "V01", "C0")]), (1, [])]:
        config = cfg(runs_per_case=runs)
        _, ctx = compare_rows(rows_for(results, config, the_cases), results, the_cases, config)
        assert ctx.at_threshold == expected


# --- the selection does not look at the comparison ---------------------------------------------

def test_comparison_changes_neither_scores_nor_statuses():
    config = cfg()
    the_cases = cases(30, critical=set(range(0, 30, 3)))
    results = (answers("BASE", "o" * 30) + answers("V01", "o" * 25 + "x" * 5)
               + answers("V02", "x" * 4 + "o" * 26, model="n"))
    plain = rows_for(results, config, the_cases)
    compared, _ = compare_rows(plain, results, the_cases, config)
    strip = lambda rows: [(r.model, r.variant, r.score, r.lo, r.hi, r.status) for r in rows]
    assert strip(compared) == strip(plain)
    assert strip(select(compared, 0.7).rows) == strip(select(plain, 0.7).rows)
    assert any(r.comparison.critical_regressions for r in compared)           # V02 breaks critical cases...
    assert select(compared, 0.6).winner.variant == "V02"          # ...and wins: select() alone never filters


def fake_outcome(tmp_path, the_cases):
    config = load_config("config.example.yaml")
    store = RunStore(tmp_path / "runs")
    plan = prepare(config, the_cases, load_prices("prices.yaml"), store, dry_run=True)
    runtimes = {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected for c in the_cases}, seed=0), None)
                for m in config.models}
    return execute_plan(plan, runtimes, store)


def test_end_to_end_critical_flags_leave_the_selection_and_validation_unchanged(tmp_path):
    plain, _ = load_cases("cases.example.csv")
    flagged = [dataclasses.replace(c, critical=i % 2 == 0) for i, c in enumerate(plain)]
    a, b = fake_outcome(tmp_path / "a", plain), fake_outcome(tmp_path / "b", flagged)
    strip = lambda o: ([(r.model, r.variant, r.score, r.lo, r.hi, r.status) for r in o.rows],
                       [(v.model, v.variant, v.score, v.confirmed) for v in o.validations])
    assert strip(a) == strip(b)
    assert b.comparison.reference == ("modele-exemple-anthropic", "BASE")
    tested = [r for r in b.rows if r.status not in (NON_TESTE, SANS_REPONSE)]
    assert tested and all(r.comparison is not None for r in tested)
    assert all(r.status for r in b.rows)                     # after select every row has a status
    assert sum(r.critical_n for r in b.rows) > 0 and sum(r.critical_n for r in a.rows) == 0
