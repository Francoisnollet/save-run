"""Confirmation before paid validation calls: engine callback and CLI prompt. Fake clients only, never the real .env."""
import dataclasses

import pytest

import saverun.cli as cli
import saverun.pipeline as pipeline
from saverun.cli import main
from saverun.config import load_config
from saverun.cost import call_cost, estimate_tokens, load_prices
from saverun.fake import FakeClient
from saverun.pipeline import VALIDATION_DECLINED, execute_plan, prepare
from saverun.runner import ModelRuntime, RunStore, build_jobs, execute
from saverun.splitting import load_cases

DECLINED_START = VALIDATION_DECLINED.split("{")[0]


def example(tmp_path, store_dir=None, **cfg_over):
    cfg = dataclasses.replace(load_config("config.example.yaml"), **cfg_over)
    cases, _ = load_cases("cases.example.csv")
    prices = load_prices("prices.yaml")
    store = RunStore(store_dir or tmp_path / "runs")
    plan = prepare(cfg, cases, prices, store, dry_run=False)
    return cfg, cases, prices, store, plan


def fakes(cfg, cases):
    return {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected for c in cases}, seed=0), None)
            for m in cfg.models}


class NoCall:
    def complete(self, *a, **k):
        pytest.fail("no call expected: everything should come from the store")


def no_calls(cfg):
    return {m.id: ModelRuntime(NoCall(), None) for m in cfg.models}


def lines(store):
    return store.path.read_text(encoding="utf-8").splitlines() if store.path.exists() else []


# --- engine ----------------------------------------------------------------------------

def test_declining_makes_no_validation_call_and_leaves_the_winner_unconfirmed(tmp_path):
    cfg, cases, _, store, plan = example(tmp_path)
    asked = []
    outcome = execute_plan(plan, fakes(cfg, cases), store, confirm_validation=lambda q: asked.append(q) or False)
    assert len(asked) == 1 and outcome.selection.winner is not None
    assert len(lines(store)) == len(plan.jobs)                        # selection only, no validation call
    assert not outcome.validation_results and outcome.validations == []
    assert sum(n.startswith(DECLINED_START) for n in outcome.notes) == 1
    assert not any("n'a pas pu être exécutée" in n for n in outcome.notes)   # one note, not two


def test_the_quote_is_priced_at_the_candidate_model_not_the_worst_case(tmp_path):
    cfg, cases, prices, store, plan = example(tmp_path)
    asked = []
    execute_plan(plan, fakes(cfg, cases), store, confirm_validation=lambda q: asked.append(q) or False)
    (quote,) = asked[0]
    w = (quote.model, quote.variant)
    jobs = build_jobs(plan.all_variants, cfg, plan.split.validation, "validation", [w])
    by_hand = sum(call_cost(estimate_tokens(j.system + j.input), cfg.max_tokens, prices.models[quote.model])
                  for j in jobs)
    assert quote.is_winner and quote.new_calls == len(plan.split.validation) * cfg.runs_per_case == len(jobs)
    assert quote.max_cost == pytest.approx(by_hand) and quote.estimated is prices.models[quote.model].free
    assert quote.max_cost < plan.validation_cost_ceiling          # the worst case over every model is dearer here


def test_accepting_runs_the_validation(tmp_path):
    cfg, cases, _, store, plan = example(tmp_path)
    outcome = execute_plan(plan, fakes(cfg, cases), store, confirm_validation=lambda q: True)
    assert len(outcome.validation_results) == len(plan.split.validation) and outcome.validations
    assert not any(n.startswith(DECLINED_START) for n in outcome.notes)


def test_not_asked_when_the_validation_is_cached_or_there_is_no_winner(tmp_path):
    cfg, cases, _, store, plan = example(tmp_path)
    first = execute_plan(plan, fakes(cfg, cases), store)              # validation now in the store
    again = execute_plan(plan, no_calls(cfg), store, confirm_validation=lambda q: pytest.fail("must not ask"))
    assert [v.score for v in again.validations] == [v.score for v in first.validations]

    cfg, cases, _, store, plan = example(tmp_path, store_dir=tmp_path / "other", seuil_qualite=0.999)
    outcome = execute_plan(plan, fakes(cfg, cases), store, confirm_validation=lambda q: pytest.fail("must not ask"))
    assert outcome.selection.winner is None


def test_declining_still_validates_a_winner_already_in_the_store(tmp_path, monkeypatch):
    cfg, cases, _, store, plan = example(tmp_path)
    execute_plan(plan, fakes(cfg, cases), store)                      # winner validated and cached, no near-tie
    real_select = pipeline.select

    def with_a_near_tie(rows, threshold):
        sel = real_select(rows, threshold)
        other = next(r for r in sel.rows if (r.model, r.variant) != (sel.winner.model, sel.winner.variant))
        return dataclasses.replace(sel, near_ties=[other])

    monkeypatch.setattr(pipeline, "select", with_a_near_tie)
    before = len(lines(store))
    asked = []
    outcome = execute_plan(plan, no_calls(cfg), store, confirm_validation=lambda q: asked.append(q) or False)
    winner_quote, tie_quote = asked[0]
    assert winner_quote.new_calls == 0 and tie_quote.new_calls == len(plan.split.validation)
    assert len(lines(store)) == before                                # nothing called, nothing written
    assert [v.is_winner for v in outcome.validations] == [True]       # the near-tie is not reported as validated


# --- command line ----------------------------------------------------------------------

def cached_selection(tmp_path, monkeypatch):
    """The Banking77 threshold case: every selection call in the store, the validation still to make."""
    monkeypatch.setenv("SAVERUN_ANTHROPIC_KEY", "x")
    monkeypatch.setenv("EXEMPLE_API_KEY", "x")
    cfg, cases, _, store, plan = example(tmp_path, store_dir=tmp_path / "out" / "runs")
    execute(plan.jobs, fakes(cfg, cases), store)
    monkeypatch.setattr(cli, "build_runtimes", lambda config, **kw: fakes(config, cases))
    return plan, store


def run_args(tmp_path, *extra):
    return ["run", "--config", "config.example.yaml", "--cases", "cases.example.csv", "--prices", "prices.yaml",
            "--out", str(tmp_path / "out"), "--env", str(tmp_path / "absent.env"), *extra]


def answering(monkeypatch, answer):
    questions = []

    def fake_input(q):
        questions.append(q)
        if answer is EOFError:
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return questions


def test_cli_asks_before_validation_even_with_no_selection_call(tmp_path, monkeypatch, capsys):
    plan, store = cached_selection(tmp_path, monkeypatch)
    n = len(lines(store))
    questions = answering(monkeypatch, "n")
    assert main(run_args(tmp_path)) == 0
    assert questions == ["Lancer la validation ? Ces appels sont payants. [o/N] "]   # no selection prompt
    out = capsys.readouterr().out
    assert "V08 sur modele-exemple-anthropic (gagnant) : 7 appels, coût maximum estimé" in out
    assert "Total : 7 appels" in out and DECLINED_START in out
    assert len(lines(store)) == n                                     # no validation call
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "La validation n'a pas pu être exécutée" in report


def test_cli_validates_after_a_yes(tmp_path, monkeypatch):
    plan, store = cached_selection(tmp_path, monkeypatch)
    n = len(lines(store))
    answering(monkeypatch, "o")
    assert main(run_args(tmp_path)) == 0
    assert len(lines(store)) == n + len(plan.split.validation)


def test_cli_closed_input_is_a_refusal(tmp_path, monkeypatch):
    plan, store = cached_selection(tmp_path, monkeypatch)
    n = len(lines(store))
    answering(monkeypatch, EOFError)
    assert main(run_args(tmp_path)) == 0 and len(lines(store)) == n

    monkeypatch.setenv("SAVERUN_ANTHROPIC_KEY", "x")
    monkeypatch.setenv("EXEMPLE_API_KEY", "x")
    fresh = tmp_path / "fresh"
    assert main(run_args(fresh)) == 1 and not (fresh / "out" / "runs").exists()   # first prompt: cancelled


def test_cli_yes_and_dry_run_never_ask(tmp_path, monkeypatch):
    plan, store = cached_selection(tmp_path, monkeypatch)
    n = len(lines(store))
    monkeypatch.setattr("builtins.input", lambda q: pytest.fail(f"must not ask: {q}"))
    assert main(run_args(tmp_path, "--yes")) == 0
    assert len(lines(store)) == n + len(plan.split.validation)
    assert main(run_args(tmp_path / "sim", "--dry-run")) == 0


def test_cli_both_prompts_when_selection_calls_remain(tmp_path, monkeypatch):
    monkeypatch.setenv("SAVERUN_ANTHROPIC_KEY", "x")
    monkeypatch.setenv("EXEMPLE_API_KEY", "x")
    cfg, cases, *_ = example(tmp_path)
    monkeypatch.setattr(cli, "build_runtimes", lambda config, **kw: fakes(config, cases))
    questions = answering(monkeypatch, "o")
    assert main(run_args(tmp_path)) == 0
    assert questions == ["Lancer ? Ces appels sont payants. [o/N] ",
                         "Lancer la validation ? Ces appels sont payants. [o/N] "]
