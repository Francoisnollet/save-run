"""Critical cases in validation: the current prompt is run on them, and a winner that breaks one is not confirmed.

Fake clients only, never the real .env. The winner of the example files is V08 on modele-exemple-anthropic, which
is also current_model: the reference (BASE on that model) is not a candidate.
"""
import csv
import dataclasses
from types import SimpleNamespace as NS

import pytest

import saverun.cli as cli
from saverun.cli import main
from saverun.config import load_config
from saverun.comparison import rates_by_couple
from saverun.cost import call_cost, estimate_tokens, load_prices
from saverun.fake import FakeClient
from saverun.pipeline import VALIDATION_DECLINED, execute_plan, prepare, reference_check_jobs
from saverun.report import render_report, validation_lines, verdict
from saverun.runner import Completion, MaxCallsError, ModelRuntime, PermanentError, RunStore, build_jobs, execute
from saverun.scoring import variant_score
from saverun.selection import Row, Validation, select
from saverun.splitting import load_cases
from saverun.variants import BASE_ID

REF_MODEL = "modele-exemple-anthropic"
REFERENCE = (REF_MODEL, BASE_ID)


def plain_cases():
    cases, _ = load_cases("cases.example.csv")
    return cases


def split_ids(**cfg_over):
    """The validation case ids of the example files: the split does not depend on the critical column."""
    cfg = dataclasses.replace(load_config("config.example.yaml"), **cfg_over)
    plan = prepare(cfg, plain_cases(), load_prices("prices.yaml"), None, dry_run=False)
    return [c.id for c in plan.split.validation], [c.id for c in plan.split.selection]


def example(tmp_path, critical=(), store_dir=None, **cfg_over):
    cfg = dataclasses.replace(load_config("config.example.yaml"), **cfg_over)
    cases = [dataclasses.replace(c, critical=c.id in critical) for c in plain_cases()]
    store = RunStore(store_dir or tmp_path / "runs")
    plan = prepare(cfg, cases, load_prices("prices.yaml"), store, dry_run=False)
    return cfg, cases, store, plan


class Scripted:
    """The fake model, except on given inputs: on `broken` the current prompt answers right and every other prompt
    wrong; on `failing`, every prompt but the current one fails for good."""

    def __init__(self, model, cases, base_system, broken=(), failing=()):
        self.fake = FakeClient(model, {c.input: c.expected for c in cases}, seed=0)
        self.expected = {c.input: c.expected for c in cases}
        self.base, self.broken, self.failing = base_system, set(broken), set(failing)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user, *, max_tokens, temperature):
        self.calls.append((system, user))
        if user in self.failing and system != self.base:
            raise PermanentError("FakeError (HTTP 400)")
        if user in self.broken or user in self.failing:
            text = self.expected[user] if system == self.base else "indéterminé"
            return Completion(text, estimate_tokens(system + user), estimate_tokens(text), False)
        return self.fake.complete(system, user, max_tokens=max_tokens, temperature=temperature)


def clients(cfg, cases, plan, broken=(), failing=()):
    inputs = {c.id: c.input for c in cases}
    made = {m.id: Scripted(m.id, cases, plan.base.system, [inputs[i] for i in broken], [inputs[i] for i in failing])
            for m in cfg.models}
    return made, {m: ModelRuntime(c, None) for m, c in made.items()}


def validation_calls(made, plan):
    """Calls made on a validation case, by any prompt."""
    inputs = {c.input for c in plan.split.validation}
    return [(s, u) for c in made.values() for s, u in c.calls if u in inputs]


def no_sleep(_):
    return None


def fingerprint(o):
    """Selection and validation, without the critical check."""
    return ([(r.model, r.variant, r.score, r.lo, r.status) for r in o.rows],
            [(v.model, v.variant, v.n_cases, v.score) for v in o.validations],
            (o.selection.winner.model, o.selection.winner.variant),
            [(t.model, t.variant) for t in o.selection.near_ties])


# --- without critical cases in validation: nothing changes ------------------------------------------------------

def test_without_critical_validation_case_no_base_call_and_the_validation_is_unchanged(tmp_path):
    val_ids, sel_ids = split_ids()
    cfg, cases, store, plain = example(tmp_path / "a")
    _, runtimes = clients(cfg, cases, plain)
    before = execute_plan(plain, runtimes, store, sleep=no_sleep)
    assert plain.reference_check_calls == 0 and plain.reference_check_cost == 0

    # critical cases in the selection only: the validation must not move
    cfg2, cases2, store2, plan2 = example(tmp_path / "b", critical=sel_ids[:3])
    _, runtimes2 = clients(cfg2, cases2, plan2)
    after = execute_plan(plan2, runtimes2, store2, sleep=no_sleep)
    assert plan2.reference_check_calls == 0
    for o in (before, after):
        winner = (o.selection.winner.model, o.selection.winner.variant)
        assert winner != REFERENCE
        assert len(o.validation_results) == len(val_ids) * cfg.runs_per_case        # the winner's calls only
        assert {(r.model, r.variant) for r in o.validation_results} == {winner}
        assert not any(v.critical_checked or v.critical_n or v.critical_regressions for v in o.validations)
    assert fingerprint(before) == fingerprint(after)
    assert [v.confirmed for v in before.validations] == [v.confirmed for v in after.validations]
    assert validation_lines(before) == validation_lines(after) and verdict(before) == verdict(after)


def test_without_critical_case_the_quote_has_no_reference_line(tmp_path):
    cfg, cases, store, plan = example(tmp_path)
    asked = []
    _, runtimes = clients(cfg, cases, plan)
    execute_plan(plan, runtimes, store, sleep=no_sleep, confirm_validation=lambda q: asked.append(q) or False)
    assert [q.is_reference for q in asked[0]] == [False]


# --- with critical cases in validation ----------------------------------------------------------------------------

def test_the_current_prompt_runs_on_the_critical_validation_cases_only(tmp_path):
    val_ids, _ = split_ids()
    critical = val_ids[:2]
    cfg, cases, store, plan = example(tmp_path, critical=critical)
    assert plan.reference_check_calls == len(critical) * cfg.runs_per_case
    made, runtimes = clients(cfg, cases, plan)
    o = execute_plan(plan, runtimes, store, sleep=no_sleep)
    ref_val = [r for r in o.validation_results if (r.model, r.variant) == REFERENCE]
    assert sorted(r.case for r in ref_val) == sorted(critical)
    (w,) = [v for v in o.validations if v.is_winner]
    assert w.critical_checked and w.critical_n == len(critical) and w.critical_regressions == ()
    assert w.confirmed


def failed_by_the_winner(tmp_path):
    """Validation cases the winner fails with the plain fake model: breaking one keeps its raw score."""
    cfg, cases, store, plan = example(tmp_path)
    _, runtimes = clients(cfg, cases, plan)
    o = execute_plan(plan, runtimes, store, sleep=no_sleep)
    w = (o.selection.winner.model, o.selection.winner.variant)
    rates = rates_by_couple(o.validation_results, {c.id: c.expected for c in plan.split.validation}, cfg,
                            "validation")[w]
    return sorted(case for case, rate in rates.items() if rate == 0)


def test_a_critical_regression_in_validation_unconfirms_the_winner_without_reselecting(tmp_path):
    val_ids, _ = split_ids()
    failed = failed_by_the_winner(tmp_path / "probe")
    assert failed                                         # the fake winner fails one validation case
    critical = [failed[0], next(i for i in val_ids if i not in failed)]
    cfg, cases, store, plan = example(tmp_path / "strict", critical=critical)
    made, runtimes = clients(cfg, cases, plan, broken=critical[:1])
    o = execute_plan(plan, runtimes, store, sleep=no_sleep)

    cfg_t, cases_t, store_t, plan_t = example(tmp_path / "tolerant", critical=critical, max_regressions_critiques=1)
    _, runtimes_t = clients(cfg_t, cases_t, plan_t, broken=critical[:1])
    tolerant = execute_plan(plan_t, runtimes_t, store_t, sleep=no_sleep)

    (w,) = [v for v in o.validations if v.is_winner]
    assert w.critical_regressions == (critical[0],) and not w.confirmed
    assert w.score >= cfg.seuil_qualite                  # the raw score alone would have confirmed it
    assert [v.confirmed for v in tolerant.validations if v.is_winner] == [True]
    assert fingerprint(o) == fingerprint(tolerant)       # same winner, same near-ties, same scores: no re-selection
    text = verdict(o)
    assert "NON CONFIRMÉE" in text and f"cas critique(s) que le prompt actuel réussissait : {critical[0]}." in text
    assert "sa note est sous le seuil" not in text
    lines = validation_lines(o)
    assert any(f"1 régression(s) sur cas critiques : {critical[0]}" in line for line in lines)
    assert any(line.startswith("Non confirmée : elle rate 1 cas critique(s)") and "re-sélectionner" in line
               for line in lines)
    assert not any("la note sur la validation est sous" in line for line in lines)
    assert "Non confirmée : elle rate 1 cas critique(s)" in render_report(o)


def test_a_failed_call_on_a_critical_case_is_unverified_not_a_regression(tmp_path):
    val_ids, _ = split_ids()
    critical = val_ids[:2]
    cfg, cases, store, plan = example(tmp_path, critical=critical)
    _, runtimes = clients(cfg, cases, plan, failing=critical[:1])
    o = execute_plan(plan, runtimes, store, sleep=no_sleep)
    (w,) = [v for v in o.validations if v.is_winner]
    assert w.critical_unverified == (critical[0],) and critical[0] not in w.critical_regressions and w.confirmed
    assert verdict(o).endswith(", confirmée sur la note de validation, mais 1 cas critique(s) de validation n'ont "
                               f"pas été comparés au prompt actuel : {critical[0]}.")
    assert "confirmée sur les cas de validation" not in verdict(o)


def test_no_base_call_when_the_reference_is_the_only_candidate(tmp_path):
    val_ids, _ = split_ids()
    cfg, cases, store, plan = example(tmp_path, critical=val_ids[:2])
    assert reference_check_jobs(plan, [REFERENCE], REFERENCE) == []
    assert reference_check_jobs(plan, [REFERENCE, (REF_MODEL, "V08")], REFERENCE)   # a near-tie to compare
    assert reference_check_jobs(plan, [(REF_MODEL, "V08")], None) == []           # no measured reference


def test_max_calls_counts_the_reference_check(tmp_path):
    val_ids, _ = split_ids()
    cfg, cases, store, plan = example(tmp_path, critical=val_ids[:2], max_calls=None)
    need = plan.preflight.new_calls + plan.validation_calls_per_candidate
    example(tmp_path / "ok", critical=val_ids[:2], max_calls=need + plan.reference_check_calls)
    with pytest.raises(MaxCallsError, match="appels du prompt actuel sur les cas critiques de validation"):
        example(tmp_path / "short", critical=val_ids[:2], max_calls=need + plan.reference_check_calls - 1)
    example(tmp_path / "plain", max_calls=need)                                   # without critical cases: as before


# --- the confirmation quote ------------------------------------------------------------------------------------

def test_the_quote_has_a_reference_line_priced_at_the_reference_model(tmp_path):
    val_ids, _ = split_ids()
    critical = val_ids[:2]
    cfg, cases, store, plan = example(tmp_path, critical=critical)
    asked = []
    made, runtimes = clients(cfg, cases, plan)
    o = execute_plan(plan, runtimes, store, sleep=no_sleep, confirm_validation=lambda q: asked.append(q) or False)
    winner_quote, ref_quote = asked[0]
    assert winner_quote.is_winner and not winner_quote.is_reference
    assert (ref_quote.model, ref_quote.variant, ref_quote.is_reference) == (REF_MODEL, BASE_ID, True)
    price = load_prices("prices.yaml").models[REF_MODEL]
    jobs = build_jobs(plan.all_variants, cfg, [c for c in plan.split.validation if c.critical], "validation",
                      [REFERENCE])
    assert ref_quote.new_calls == len(critical) * cfg.runs_per_case == len(jobs)
    assert ref_quote.max_cost == pytest.approx(sum(call_cost(estimate_tokens(j.system + j.input), cfg.max_tokens,
                                                             price) for j in jobs))
    # declined: no validation call at all, the report as before
    assert not o.validation_results and o.validations == [] and not validation_calls(made, plan)
    note = VALIDATION_DECLINED.format(n=winner_quote.new_calls + ref_quote.new_calls)
    assert note in o.notes
    assert "La validation n'a pas pu être exécutée" in verdict(o)


def test_declining_with_the_winner_cached_leaves_the_critical_cases_unverified(tmp_path):
    val_ids, _ = split_ids()
    critical = val_ids[:2]
    cfg, cases, store, plan = example(tmp_path, critical=critical)
    made, runtimes = clients(cfg, cases, plan)
    w = execute_plan(plan, runtimes, store, sleep=no_sleep, confirm_validation=lambda q: False).selection.winner
    execute(build_jobs(plan.all_variants, cfg, plan.split.validation, "validation", [(w.model, w.variant)]),
            runtimes, store, sleep=no_sleep)                                      # the winner alone is cached
    made, runtimes = clients(cfg, cases, plan)
    asked = []
    o = execute_plan(plan, runtimes, store, sleep=no_sleep, confirm_validation=lambda q: asked.append(q) or False)
    assert [(q.is_reference, q.new_calls > 0) for q in asked[0]] == [(False, False), (True, True)]
    assert not any(c.calls for c in made.values())                             # nothing called at all
    (v,) = o.validations
    assert v.critical_checked and v.critical_unverified == tuple(sorted(critical)) and v.confirmed
    assert verdict(o).endswith(", confirmée sur la note de validation, mais 2 cas critique(s) de validation n'ont "
                               f"pas été comparés au prompt actuel : {', '.join(sorted(critical))}.")


def test_unverified_critical_cases_of_a_winner_not_confirmed_are_still_named():
    score, lo, hi = variant_score([1] * 95 + [0] * 5)
    w = Row(model="m", variant="V01", description="V01", n_cases=100, n_planned=100, score=score, lo=lo, hi=hi,
            avg_in=100, avg_out=5, cost_per_1000=0.1, estimated=False, failed_share=0.0, truncated_share=0.0)
    sel = select([w], 0.75)
    v = Validation("m", "V01", 7, 7, 0.5, 0.2, 0.8, False, True, True, critical_n=2, critical_checked=True,
                   critical_unverified=("C3",))
    o = NS(selection=sel, rows=sel.rows, validations=[v],
           plan=NS(config=NS(seuil_qualite=0.75, max_regressions_critiques=0), prices=NS(currency="EUR")))
    assert verdict(o).endswith("NON CONFIRMÉE sur les cas de validation : ajoutez des cas avant de l'appliquer. "
                               "1 cas critique(s) de validation non vérifié(s) (appel en échec, ou prompt actuel "
                               "non exécuté) : C3.")


def test_cli_prints_the_reference_line_of_the_quote(tmp_path, monkeypatch, capsys):
    val_ids, _ = split_ids()
    critical = set(val_ids[:2])
    path = tmp_path / "cases.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        out = csv.writer(f)
        out.writerow(["input", "expected", "critical"])
        out.writerows([c.input, c.expected, "oui" if c.id in critical else "non"] for c in plain_cases())
    flagged, _ = load_cases(path)
    assert {c.id for c in flagged if c.critical} == critical
    monkeypatch.setenv("SAVERUN_ANTHROPIC_KEY", "x")
    monkeypatch.setenv("EXEMPLE_API_KEY", "x")
    monkeypatch.setattr(cli, "build_runtimes",
                        lambda config, **kw: {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected
                                                                                   for c in flagged}, seed=0), None)
                                              for m in config.models})
    answers = iter(["o", "n"])
    monkeypatch.setattr("builtins.input", lambda q: next(answers))
    args = ["run", "--config", "config.example.yaml", "--cases", str(path), "--prices", "prices.yaml",
            "--out", str(tmp_path / "out"), "--env", str(tmp_path / "absent.env")]
    assert main(args) == 0
    out = " ".join(capsys.readouterr().out.split())                      # rich wraps long lines
    assert f"Cas critiques de validation : au plus {len(critical)} appels du prompt actuel sur {REF_MODEL}" in out
    assert (f"BASE sur {REF_MODEL} (prompt actuel, sur les cas critiques de validation seulement) : "
            f"{len(critical)} appels, coût maximum estimé") in out
