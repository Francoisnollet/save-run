"""Selection with critical cases: a variant that breaks a critical case passed by the current prompt is set aside."""
import dataclasses
from types import SimpleNamespace as NS

import pytest

from saverun.comparison import Comparison
from saverun.config import load_config
from saverun.cost import load_prices
from saverun.fake import FakeClient
from saverun.pipeline import execute_plan, prepare
from saverun.report import render_report, set_aside_sentence, verdict
from saverun.runner import ModelRuntime, RunStore
from saverun.scoring import variant_score
from saverun.selection import (CASSE_CRITIQUE, PLUS_CHERE, QUASI, RETENUE, SOUS_SEUIL, Row, select,
                               set_aside_critical, validation_pairs)
from saverun.splitting import load_cases


def row(variant, cost, k=95, n=100, critical=(), compared=True, **over):
    score, lo, hi = variant_score([1] * k + [0] * (n - k))
    comparison = Comparison(regressions=tuple(critical), critical_regressions=tuple(critical)) if compared else None
    fields = dict(model="m", variant=variant, description=variant, n_cases=n, n_planned=n, score=score, lo=lo, hi=hi,
                  avg_in=100, avg_out=5, cost_per_1000=cost, estimated=False, failed_share=0.0, truncated_share=0.0,
                  comparison=comparison)
    return Row(**{**fields, **over})


def pick(rows, threshold, max_critical=None):
    """What the pipeline does: set aside the critical breakers (when a maximum is given), then select."""
    return select(rows if max_critical is None else set_aside_critical(rows, threshold, max_critical), threshold)


def statuses(sel):
    return {r.variant: r.status for r in sel.rows}


# --- the filter -------------------------------------------------------------------------------------------

def test_a_cheaper_variant_breaking_a_critical_case_is_set_aside_and_the_next_one_wins():
    rows = [row("V01", 0.10, critical=("C7",)), row("V02", 0.20), row("V03", 0.30)]
    sel = pick(rows, 0.75, 0)
    assert sel.winner.variant == "V02"
    assert statuses(sel) == {"V01": CASSE_CRITIQUE, "V02": RETENUE, "V03": PLUS_CHERE}
    assert validation_pairs(sel) == [("m", "V02")]


def test_a_variant_set_aside_is_never_a_near_tie():
    rows = [row("V01", 0.100), row("V02", 0.105, critical=("C7",)), row("V03", 0.108)]
    sel = pick(rows, 0.75, 0)
    assert statuses(sel) == {"V01": RETENUE, "V02": CASSE_CRITIQUE, "V03": QUASI}
    assert [r.variant for r in sel.near_ties] == ["V03"]


def test_the_filter_only_touches_rows_that_would_have_been_eligible():
    rows = [row("V01", 0.10, k=50, critical=("C7",)), row("V02", 0.20)]
    sel = pick(rows, 0.75, 0)
    assert statuses(sel) == {"V01": SOUS_SEUIL, "V02": RETENUE}


def test_max_critical_regressions_is_a_tolerance():
    rows = [row("V01", 0.10, critical=("C1", "C2")), row("V02", 0.20, critical=("C1",)), row("V03", 0.30)]
    assert pick(rows, 0.75, 0).winner.variant == "V03"
    assert pick(rows, 0.75, 1).winner.variant == "V02"
    assert statuses(pick(rows, 0.75, 1))["V01"] == CASSE_CRITIQUE
    assert pick(rows, 0.75, 2).winner.variant == "V01"


def test_without_comparison_or_without_max_nothing_is_filtered():
    rows = [row("V01", 0.10, compared=False), row("V02", 0.20)]
    assert pick(rows, 0.75, 0).winner.variant == "V01"                        # no reference: cannot be checked
    breaking = [row("V01", 0.10, critical=("C7",)), row("V02", 0.20)]
    assert pick(breaking, 0.75).winner.variant == "V01"                       # max_critical not given
    assert CASSE_CRITIQUE not in statuses(pick(breaking, 0.75)).values()


def test_every_eligible_variant_breaking_a_critical_case_means_no_winner():
    sel = pick([row("V01", 0.10, critical=("C7",)), row("V02", 0.20, critical=("C8",))], 0.75, 0)
    assert sel.winner is None and sel.near_ties == [] and validation_pairs(sel) == []


# --- the verdict --------------------------------------------------------------------------------------------

def outcome(rows, max_critical=0):
    sel = pick(rows, 0.75, max_critical)
    return NS(selection=sel, rows=sel.rows, validations=[],
              plan=NS(config=NS(seuil_qualite=0.75), prices=NS(currency="EUR")))


def test_verdict_names_the_cheapest_variant_set_aside_and_its_critical_cases():
    o = outcome([row("V05", 0.05, critical=("C007", "C012", "C020", "C031", "C044", "C050", "C061")),
                 row("V06", 0.06, critical=("C007",)), row("V02", 0.20), row("V09", 0.01, k=10, critical=("C1",))])
    assert set_aside_sentence(o) == (" V05 sur m était moins chère (0,050 EUR), mais elle rate 7 cas critique(s) que "
                                     "le prompt actuel réussissait : C007, C012, C020, C031, C044 … et 2 autres ; "
                                     "1 autre(s) variante(s) moins chère(s) écartée(s) pour la même raison.")
    assert verdict(o).startswith("V02 sur m est la moins chère") and verdict(o).endswith(set_aside_sentence(o))


def test_no_sentence_when_the_variant_set_aside_is_dearer_than_the_winner():
    o = outcome([row("V01", 0.10), row("V02", 0.20, critical=("C7",))])
    assert set_aside_sentence(o) == "" and "critique" not in verdict(o)


def test_verdict_without_winner_because_of_critical_cases():
    o = outcome([row("V01", 0.10, critical=("C7",)), row("V02", 0.20, critical=("C8", "C9"))])
    text = verdict(o)
    assert text.startswith("Aucune variante n'est retenue : celles qui démontrent 75 % de qualité")
    assert text.endswith(" La moins chère, V01 sur m (0,100 EUR), rate 1 cas critique(s) : C7 ; "
                         "1 autre(s) variante(s) écartée(s) pour la même raison.")
    assert "baissez le seuil" not in text


# --- end to end on the fake model ---------------------------------------------------------------------------

def run(tmp_path, critical=lambda i: False, **cfg_over):
    config = dataclasses.replace(load_config("config.example.yaml"), **cfg_over)
    plain, _ = load_cases("cases.example.csv")
    cases = [dataclasses.replace(c, critical=critical(i)) for i, c in enumerate(plain)]
    store = RunStore(tmp_path / "runs")
    plan = prepare(config, cases, load_prices("prices.yaml"), store, dry_run=True)
    runtimes = {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected for c in cases}, seed=0), None)
                for m in config.models}
    return execute_plan(plan, runtimes, store)


def fingerprint(o):
    return ([(r.model, r.variant, r.score, r.lo, r.status) for r in o.rows],
            [(v.model, v.variant, v.score, v.confirmed) for v in o.validations], verdict(o))


@pytest.mark.parametrize("threshold", [0.5, 0.75])
def test_without_the_critical_column_no_verdict_changes(tmp_path, threshold):
    strict = run(tmp_path / "a", seuil_qualite=threshold, max_regressions_critiques=0)
    off = run(tmp_path / "b", seuil_qualite=threshold, max_regressions_critiques=10**6)
    assert fingerprint(strict) == fingerprint(off)
    assert CASSE_CRITIQUE not in {r.status for r in strict.rows}


def test_with_critical_cases_the_cheap_breakers_are_set_aside_end_to_end(tmp_path):
    o = run(tmp_path, critical=lambda i: i % 3 == 0, seuil_qualite=0.5)
    off = run(tmp_path / "off", critical=lambda i: i % 3 == 0, seuil_qualite=0.5, max_regressions_critiques=10**6)
    aside = [r for r in o.rows if r.status == CASSE_CRITIQUE]
    assert aside and all(r.comparison.critical_regressions for r in aside)
    w = o.selection.winner
    assert w is not None and not w.comparison.critical_regressions
    assert all(not t.comparison.critical_regressions for t in o.selection.near_ties)
    assert off.selection.winner.cost_per_1000 < w.cost_per_1000                  # the filter changed the winner here
    assert [(r.model, r.variant, r.score) for r in o.rows] == [(r.model, r.variant, r.score) for r in off.rows]
    report = render_report(o)
    assert set_aside_sentence(o) and set_aside_sentence(o) in report.split("## Verdict")[1].split("##")[0]
    assert f"| {CASSE_CRITIQUE} |" in report


def test_without_base_prompt_the_filter_does_not_apply_and_the_report_warns(tmp_path):
    o = run(tmp_path, critical=lambda i: i % 3 == 0, seuil_qualite=0.5, base_prompt=None)
    assert CASSE_CRITIQUE not in {r.status for r in o.rows}
    assert "aucune régression sur un cas critique ne peut être détectée" in render_report(o)
