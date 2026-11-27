"""Outputs of the case-by-case comparison: results.csv, comparison.csv, report.md, warnings. Fake model only."""
import csv
import dataclasses
from types import SimpleNamespace as NS

import pytest

from saverun.comparison import Comparison, ComparisonContext
from saverun.config import load_config
from saverun.cost import load_prices
from saverun.fake import FakeClient
from saverun.pipeline import execute_plan, prepare
from saverun.report import (COMPARISON_CSV, CSV_FIELDS, OTHER_CRITICAL_SHOWN, at_threshold_lines, comparison_warnings,
                            detail_lines, render_report, verdict, winner_comparison_line, write_outputs, _case_details,
                            _ids)
from saverun.runner import ModelRuntime, RunStore
from saverun.scoring import variant_score
from saverun.selection import Row, select
from saverun.splitting import (MIN_CRITICAL_SELECTION, MIN_CRITICAL_VALIDATION, Case, Split, critical_warnings,
                               load_cases)

NEW_COLUMNS = ["regressions", "critical_regressions", "corrections", "common_failures", "unverified",
               "critical_passed", "critical_n", "critical_score", "critical_ci_low", "critical_ci_high", "at_threshold"]


def run(tmp_path, critical=lambda i: False, **cfg_over):
    """A fake run on the example files; `critical(i)` flags the i-th case of cases.example.csv."""
    config = dataclasses.replace(load_config("config.example.yaml"), **cfg_over)
    plain, _ = load_cases("cases.example.csv")
    cases = [dataclasses.replace(c, critical=critical(i)) for i, c in enumerate(plain)]
    store = RunStore(tmp_path / "runs")
    plan = prepare(config, cases, load_prices("prices.yaml"), store, dry_run=True)
    runtimes = {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected for c in cases}, seed=0), None)
                for m in config.models}
    return execute_plan(plan, runtimes, store)


def written(tmp_path, outcome):
    paths, notes = write_outputs(outcome, tmp_path / "out")
    with (tmp_path / "out" / "results.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {p.name for p in paths}, notes, rows, (tmp_path / "out" / "report.md").read_text(encoding="utf-8")


def every_third(i):
    return i % 3 == 0


# --- results.csv and comparison.csv ------------------------------------------------------------------------

def test_results_csv_keeps_its_columns_and_adds_the_comparison_at_the_end(tmp_path):
    names, _, rows, _ = written(tmp_path, run(tmp_path, every_third))
    header = list(rows[0])
    assert header == CSV_FIELDS and header[-len(NEW_COLUMNS):] == NEW_COLUMNS
    assert header.index("off_format_share") == header.index("truncated_share") + 1
    winner = next(r for r in rows if r["status"] == "retenue")
    assert winner["regressions"] != "" and winner["critical_n"] != "" and winner["at_threshold"] == ""  # 1 run
    assert COMPARISON_CSV in names


def test_comparison_csv_has_one_line_per_couple_and_selection_case(tmp_path):
    o = run(tmp_path, every_third)
    written(tmp_path, o)
    with (tmp_path / "out" / COMPARISON_CSV).open(encoding="utf-8") as f:
        lines = list(csv.DictReader(f))
    compared = [r for r in o.rows if r.comparison is not None]
    assert len(lines) == len(compared) * len(o.plan.split.selection)
    critical = {c.id for c in o.plan.split.selection if c.critical}
    for r in compared:
        mine = [x for x in lines if (x["model"], x["variant"]) == (r.model, r.variant)]
        assert sorted(x["case"] for x in mine if x["outcome"] == "régression") == list(r.comparison.regressions)
        assert sorted(x["case"] for x in mine if x["outcome"] == "correction") == list(r.comparison.corrections)
        assert {x["case"] for x in mine if x["critical"] == "True"} == critical


def test_without_base_prompt_nothing_is_compared(tmp_path):
    names, notes, rows, report = written(tmp_path, run(tmp_path, base_prompt=None))
    assert COMPARISON_CSV not in names and not any(COMPARISON_CSV in n for n in notes)   # no critical case: no note
    assert all(r[c] == "" for r in rows for c in NEW_COLUMNS)
    section = report.split("## Par rapport au prompt actuel")[1].split("## ")[0]
    assert "Aucune comparaison cas par cas" in section and "base_prompt" in section and "|" not in section


def test_without_base_but_with_critical_cases_the_report_warns(tmp_path):
    o = run(tmp_path, every_third, base_prompt=None)
    (warning,) = comparison_warnings(o)
    assert "aucune régression sur un cas critique ne peut être détectée" in warning
    report = render_report(o)
    assert warning in report.split("## Avertissements")[1]
    names, notes, _, _ = written(tmp_path, o)
    assert COMPARISON_CSV not in names and any(COMPARISON_CSV in n for n in notes)
    assert "Cas critiques réussis (intervalle 95 %)" in report and "Régressions" not in report


def test_without_the_critical_column_no_critical_output(tmp_path):
    o = run(tmp_path)
    _, _, rows, report = written(tmp_path, o)
    assert all(r["critical_n"] == "" and r["critical_score"] == "" for r in rows)
    assert all(r["regressions"] != "" for r in rows)                      # the comparison itself is still there
    assert "Cas critiques réussis" not in report and comparison_warnings(o) == []
    assert not any("critique" in w for w in o.plan.warnings)


def test_the_comparison_changes_no_verdict(tmp_path):
    a, b = run(tmp_path / "a"), run(tmp_path / "b", every_third)
    assert verdict(a) == verdict(b)
    assert [(r.model, r.variant, r.status) for r in a.rows] == [(r.model, r.variant, r.status) for r in b.rows]


# --- the report section ------------------------------------------------------------------------------------

def test_section_states_the_reference_and_the_success_rule(tmp_path):
    report = render_report(run(tmp_path, every_third))
    section = report.split("## Par rapport au prompt actuel")[1].split("## Conditions")[0]
    assert "Référence : BASE sur modele-exemple-anthropic" in section and COMPARISON_CSV in section
    assert "la note et l'intervalle de Wilson n'en dépendent pas" in section
    assert "Avec un seul passage par cas" in section
    assert "| BASE (référence) | modele-exemple-anthropic |" in section
    assert report.index("## Résultats") < report.index("## Par rapport au prompt actuel") < report.index(
        "## Conditions et réserves")


def row(variant="V01", model="m", comparison=None, k=0, n=0, **over):
    score, lo, hi = variant_score([1] * 9 + [0])
    fields = dict(model=model, variant=variant, description=variant, n_cases=10, n_planned=10, score=score, lo=lo,
                  hi=hi, avg_in=100, avg_out=5, cost_per_1000=0.1, estimated=False, failed_share=0.0,
                  truncated_share=0.0, comparison=comparison, critical_passed=k, critical_n=n)
    return Row(**{**fields, **over})


def ids(prefix, n):
    return tuple(f"{prefix}{i:03d}" for i in range(n))


def test_critical_regressions_are_listed_in_full_and_the_rest_cut_at_ten():
    c = Comparison(regressions=tuple(sorted(ids("A", 12) + ids("C", 13))), critical_regressions=ids("C", 13),
                   corrections=ids("K", 11), common_failures=ids("E", 3))
    line = _case_details(row(comparison=c), False, set(ids("C", 13)))
    assert "régressions sur cas critiques (13) : " + ", ".join(ids("C", 13)) in line
    assert "autres régressions (12) : " + ", ".join(ids("A", 10)) + " … et 2 autres" in line
    assert "corrections (11) : " in line and "K009 … et 1 autres" in line
    assert "échecs communs (3) : E000, E001, E002" in line


def test_reference_row_lists_its_own_failures_critical_ones_in_full():
    c = Comparison(common_failures=tuple(sorted(ids("A", 11) + ids("C", 12))))
    line = _case_details(row("BASE", comparison=c), True, set(ids("C", 12)))
    assert "cas critiques ratés (12) : " + ", ".join(ids("C", 12)) in line
    assert "autres cas ratés (11) : " in line and "… et 1 autres" in line and "régression" not in line


def test_unverified_critical_cases_are_listed_in_full():
    c = Comparison(unverified=ids("C", 12), critical_unverified=ids("C", 12))
    assert "cas critiques non vérifiés (12) : " + ", ".join(ids("C", 12)) + "." in _case_details(
        row(comparison=c), False, set(ids("C", 12)))
    assert _case_details(row(comparison=Comparison()), False, set()) is None


def test_ids_of_other_critical_breakers_are_capped_in_table_order(tmp_path):
    o = run(tmp_path, every_third)
    details = render_report(o).split("Identifiants des cas")[1].split("## Conditions")[0]
    bullets = [line for line in details.splitlines() if line.startswith("- ")]
    key = {(o.selection.winner.model, o.selection.winner.variant), ("modele-exemple-anthropic", "BASE"),
           *((t.model, t.variant) for t in o.selection.near_ties)}
    others = [r for r in o.rows if r.comparison is not None and (r.model, r.variant) not in key
              and r.comparison.critical_regressions]
    assert len(others) > OTHER_CRITICAL_SHOWN                              # the fake run breaks many critical cases
    expected = [f"- **{r.variant} sur {r.model}** : régressions sur cas critiques "
                f"({len(r.comparison.critical_regressions)}) : {', '.join(r.comparison.critical_regressions)}."
                for r in others[:OTHER_CRITICAL_SHOWN]]
    expected.append(f"- … et {len(others) - OTHER_CRITICAL_SHOWN} autres variantes : voir {COMPARISON_CSV}.")
    assert bullets[-len(expected):] == expected


def details_outcome(n_others):
    """A winner and a near-tie with regressions, the reference, and n_others variants breaking a critical case."""
    winner = row("V01", cost_per_1000=0.10,
                 comparison=Comparison(regressions=("A1", "C1"), critical_regressions=("C1",), corrections=("K1",)))
    tie = row("V02", cost_per_1000=0.105, comparison=Comparison(regressions=("A2",)))
    ref = row("BASE", cost_per_1000=0.5, comparison=Comparison(common_failures=("A9", "C9")))
    plain = row("V90", cost_per_1000=0.2, comparison=Comparison(regressions=("A3",)))  # no critical regression
    others = [row(f"V{10 + i}", cost_per_1000=0.3 + i / 100, score=0.1, lo=0.0,
                  comparison=Comparison(regressions=("A4", f"C{i}"), critical_regressions=(f"C{i}",)))
              for i in range(n_others)]
    sel = select([winner, tie, ref, plain, *others], 0.5)
    return NS(rows=sel.rows, selection=sel)


def test_key_couples_in_full_then_other_critical_breakers_capped():
    o = details_outcome(10)
    assert (o.selection.winner.variant, [t.variant for t in o.selection.near_ties]) == ("V01", ["V02"])
    lines = detail_lines(o, ("m", "BASE"), {"C1", "C9", *(f"C{i}" for i in range(10))})
    assert lines[:3] == ["- **V01 sur m** : régressions sur cas critiques (1) : C1 ; autres régressions (1) : A1 ; "
                         "corrections (1) : K1.",
                         "- **V02 sur m** : régressions (1) : A2.",
                         "- **BASE sur m** : cas critiques ratés (1) : C9 ; autres cas ratés (1) : A9."]
    assert lines[3:11] == [f"- **V{10 + i} sur m** : régressions sur cas critiques (1) : C{i}." for i in range(8)]
    assert lines[11:] == [f"- … et 2 autres variantes : voir {COMPARISON_CSV}."]
    assert not any("V90" in line for line in lines)                      # no critical regression: table only


def test_no_cut_line_up_to_the_cap():
    lines = detail_lines(details_outcome(OTHER_CRITICAL_SHOWN), ("m", "BASE"), set())
    assert len(lines) == 3 + OTHER_CRITICAL_SHOWN and "autres variantes" not in lines[-1]


def test_ids_helper():
    assert _ids(["a", "b"]) == "a, b" and _ids(ids("x", 10)) == ", ".join(ids("x", 10))
    assert _ids(ids("x", 15), None).count(",") == 14


# --- cases passed just at success_rate ---------------------------------------------------------------------

def near_outcome(near, reference=("m", "BASE")):
    rows = [row("V01", cost_per_1000=0.10), row("V02", cost_per_1000=0.105), row("V03", cost_per_1000=0.2),
            row("V04", cost_per_1000=0.3, score=0.1, lo=0.0), row("BASE", cost_per_1000=0.5)]
    sel = select(rows, 0.5)
    cfg = NS(success_rate=0.6, runs_per_case=5)
    return NS(rows=sel.rows, selection=sel, plan=NS(config=cfg),
              comparison=ComparisonContext(reference, None, near))


SENTENCE = ("Une référence réussie de justesse rend les régressions moins sûres : sur ces cas, un passage de "
            "plus ou de moins suffit à créer ou à effacer une régression.")


def test_at_threshold_total_then_winner_near_ties_and_reference():
    near = sorted([("m", "V01", "C001"), ("m", "V01", "C002"), ("m", "V02", "C003"),
                   *[("m", "V03", f"C{i:03d}") for i in range(100, 140)], ("m", "BASE", "C007"), ("m", "BASE", "C008")])
    lines = at_threshold_lines(near_outcome(near))
    assert ("Au total : 45 réussite(s) de justesse, réparties sur 4 couple(s) (un même cas compte une fois par "
            "couple). Le nombre par couple est dans la colonne at_threshold") in lines[1]
    assert lines[2] == ""
    assert lines[3:] == ["- V01 sur m (gagnant) : 2 cas : C001, C002.", "- V02 sur m (quasi-égal) : 1 cas : C003.",
                         "- BASE sur m (référence) : 2 cas.", "", SENTENCE]
    assert "V03" not in "".join(lines) and "C100" not in "".join(lines)


def test_at_threshold_without_any_for_the_reference_or_the_winner():
    lines = at_threshold_lines(near_outcome([("m", "V03", "C100")]))
    assert lines[3:] == ["- V01 sur m (gagnant) : aucun cas.", "- V02 sur m (quasi-égal) : aucun cas.",
                         "- BASE sur m (référence) : aucun cas."]
    assert SENTENCE not in lines


def test_at_threshold_when_the_reference_is_the_winner():
    lines = at_threshold_lines(near_outcome([("m", "V01", "C001")], reference=("m", "V01")))
    assert lines[3:] == ["- V01 sur m (gagnant, la référence elle-même) : 1 cas : C001.",
                         "- V02 sur m (quasi-égal) : aucun cas.", "", SENTENCE]


def test_no_at_threshold_line_when_none():
    assert at_threshold_lines(near_outcome([])) == []


def test_at_threshold_column_with_several_runs(tmp_path):
    o = run(tmp_path, runs_per_case=5, success_rate=0.6, n_variants=4, max_calls=None)
    _, _, rows, report = written(tmp_path, o)
    counts = {(r["model"], r["variant"]): r["at_threshold"] for r in rows}
    assert all(v != "" for v in counts.values())
    assert o.comparison.at_threshold and "Cas réussis de justesse" in report     # 3 runs out of 5 happen
    m, v, _ = o.comparison.at_threshold[0]
    assert int(counts[(m, v)]) == sum(1 for x in o.comparison.at_threshold if x[:2] == (m, v))
    assert "Avec un seul passage par cas" not in report


# --- warnings ------------------------------------------------------------------------------------------------

def warn_outcome(k, n, threshold=0.75):
    c = Comparison(common_failures=("C1", "C2", "C9"))
    ref = row("BASE", comparison=c, k=k, n=n)
    split = NS(selection=[Case("C1", "a", "x", critical=True), Case("C2", "b", "x", critical=True),
                          Case("C9", "c", "x")])
    return NS(rows=[ref], comparison=ComparisonContext(("m", "BASE"), None),
              plan=NS(config=NS(seuil_qualite=threshold), split=split))


def test_base_failing_critical_cases_is_flagged_with_their_ids():
    (w,) = comparison_warnings(warn_outcome(2, 4))
    assert "ne réussit que 2/4 cas critiques (50 %)" in w and "sous le seuil de qualité de 75 %" in w
    assert w.endswith("Cas critiques qu'il rate : C1, C2.")
    assert comparison_warnings(warn_outcome(3, 4)) == []                  # 75 %: at the threshold, no warning


def split_of(examples=(), selection=(), validation=()):
    make = lambda prefix, flags: [Case(f"{prefix}{i}", f"{prefix}{i}", "x", critical=f) for i, f in enumerate(flags)]
    return Split(make("E", examples), make("S", selection), make("V", validation))


def test_split_warnings_on_critical_cases():
    assert critical_warnings(split_of([False] * 3, [False] * 20, [False] * 5)) == []
    enough = split_of([False] * 3, [True] * MIN_CRITICAL_SELECTION, [True] * MIN_CRITICAL_VALIDATION)
    assert critical_warnings(enough) == []
    ws = critical_warnings(split_of([True, False, True], [True] * (MIN_CRITICAL_SELECTION - 1), [False] * 5))
    assert ws == ["Cas critique(s) tiré(s) comme exemples few-shot, donc jamais évalué(s) : E0, E2.",
                  f"Seulement {MIN_CRITICAL_SELECTION - 1} cas critique(s) dans la sélection (moins de "
                  f"{MIN_CRITICAL_SELECTION}) : leur taux de réussite n'est qu'indicatif.",
                  "Aucun cas critique n'est tombé dans l'ensemble de validation."]
    (w,) = critical_warnings(split_of([False] * 3, [True] * 10, [True] * (MIN_CRITICAL_VALIDATION - 1)))
    assert f"Seulement {MIN_CRITICAL_VALIDATION - 1} cas critique(s) dans la validation" in w


def test_split_warnings_reach_the_plan_and_the_report(tmp_path):
    o = run(tmp_path, every_third)
    assert any("cas critique(s) dans la sélection" in w for w in o.plan.warnings)
    assert any("cas critique(s) dans la sélection" in w for w in render_report(o).split("## Avertissements")[1:])


# --- terminal ---------------------------------------------------------------------------------------------------

def terminal_outcome(c, critical=True):
    sel = select([row("V01", comparison=c)], 0.5)
    split = NS(selection=[Case("C1", "a", "x", critical=critical)])
    return NS(selection=sel, comparison=ComparisonContext(("m", "BASE"), None), plan=NS(split=split))


def test_terminal_says_no_regression_when_everything_is_zero():
    assert winner_comparison_line(terminal_outcome(Comparison(common_failures=("C2",)))) == (
        "Par rapport au prompt actuel : aucune régression.")
    assert winner_comparison_line(terminal_outcome(Comparison(corrections=("C2",)))) == (
        "Par rapport au prompt actuel : 0 régression(s), dont 0 sur cas critiques, et 1 correction(s). "
        "Détail dans report.md.")
    assert winner_comparison_line(terminal_outcome(Comparison(regressions=("C2",)), critical=False)) == (
        "Par rapport au prompt actuel : 1 régression(s) et 0 correction(s). Détail dans report.md.")
