"""End-to-end and report tests. Everything runs on the fake model: no network, no real key."""
import csv
import json
from types import SimpleNamespace as NS

import pytest
import yaml

from saverun.cli import main
from saverun.config import load_config, parse_config
from saverun.cost import PriceTable, load_prices
from saverun.fake import FakeClient
from saverun.pipeline import execute_plan, prepare
from saverun.report import (FOOTER, SIMULATION_BANNER, economy_lines, render_report, validation_lines, verdict,
                            write_outputs)
from saverun.runner import ModelRuntime, RunStore
from saverun.scoring import variant_score
from saverun.selection import Row, Selection, Validation, compute_economy, select
from saverun.splitting import load_cases

SECRET = "sk-SECRET-0123456789"


def example_args(tmp_path, *extra, config="config.example.yaml", prices="prices.yaml"):
    return ["run", "--config", str(config), "--cases", "cases.example.csv", "--prices", str(prices),
            "--out", str(tmp_path / "out"), "--env", str(tmp_path / "absent.env"), *extra]  # never the real .env


def rewrite(tmp_path, source, name, edit):
    data = yaml.safe_load(open(source, encoding="utf-8"))
    edit(data)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# --- dry run, end to end -----------------------------------------------------------------

def test_dry_run_end_to_end(tmp_path):
    assert main(example_args(tmp_path, "--dry-run")) == 0
    out = tmp_path / "out"
    assert (out / "results.csv").is_file() and (out / "report.md").is_file() and (out / "runs" / "simulation" / "calls.jsonl").is_file()
    assert not (out / "best_prompt.txt").exists() and not (out / "best_config.json").exists()
    report = (out / "report.md").read_text(encoding="utf-8")
    assert SIMULATION_BANNER in report and report.rstrip().endswith(FOOTER)
    rows = list(csv.DictReader((out / "results.csv").open(encoding="utf-8")))
    assert len(rows) == 21 * 2 and {r["simulation"] for r in rows} == {"True"}   # 20 variants + BASE, 2 models
    costs = [float(r["cost_per_1000"]) for r in rows]
    assert costs == sorted(costs) and sum(r["status"] == "retenue" for r in rows) == 1


def test_no_footer_and_reproducibility(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    base = ["--dry-run", "--no-footer"]
    main(example_args(a, *base)), main(example_args(b, *base)), main(example_args(c, *base, "--seed", "5"))
    ra, rb, rc = (p / "out" / "results.csv" for p in (a, b, c))
    assert ra.read_text(encoding="utf-8") == rb.read_text(encoding="utf-8")
    assert ra.read_text(encoding="utf-8") != rc.read_text(encoding="utf-8")
    assert FOOTER not in (a / "out" / "report.md").read_text(encoding="utf-8")


def test_report_states_conditions_prices_and_warnings(tmp_path):
    main(example_args(tmp_path, "--dry-run"))
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "température : défaut du fournisseur" in report          # no temperature configured
    assert "Monnaie du fichier de prix : EUR (aucune conversion)" in report
    assert "relevé du À VÉRIFIER, source : À VÉRIFIER" in report
    assert "Prix marqués « À VÉRIFIER »" in report                  # warning, since the shipped prices are examples
    assert "Aucune réponse tronquée." in report and "Aucun modèle non testé" in report
    assert "Avec 20 cas de sélection, la borne basse maximale possible est 83,9 %." in report


def test_resume_does_not_repay_calls_and_simulation_never_feeds_a_real_run(tmp_path):
    main(example_args(tmp_path, "--dry-run"))
    calls = tmp_path / "out" / "runs" / "simulation" / "calls.jsonl"
    n = len(calls.read_text(encoding="utf-8").splitlines())
    main(example_args(tmp_path, "--dry-run"))
    assert len(calls.read_text(encoding="utf-8").splitlines()) == n
    assert not (tmp_path / "out" / "runs" / "calls.jsonl").exists()   # the real store is untouched


def test_real_mode_writes_the_files_to_apply(tmp_path, monkeypatch):
    """Same pipeline with dry_run=False (still the fake model): best_prompt.txt and best_config.json appear."""
    monkeypatch.setenv("SAVERUN_ANTHROPIC_KEY", SECRET)
    cfg = load_config("config.example.yaml")
    cases, _ = load_cases("cases.example.csv")
    store = RunStore(tmp_path / "runs")
    plan = prepare(cfg, cases, load_prices("prices.yaml"), store, dry_run=False)
    runtimes = {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected for c in cases}, seed=0), None)
                for m in cfg.models}
    outcome = execute_plan(plan, runtimes, store)
    paths, notes = write_outputs(outcome, tmp_path / "out")
    names = {p.name for p in paths}
    assert names == {"results.csv", "report.md", "best_prompt.txt", "best_config.json"} and not notes
    conf = json.loads((tmp_path / "out" / "best_config.json").read_text(encoding="utf-8"))
    prompt = (tmp_path / "out" / "best_prompt.txt").read_text(encoding="utf-8")
    assert conf["simulation"] is False and conf["prompt"] + "\n" == prompt
    assert conf["model"]["id"] == outcome.selection.winner.model and conf["model"]["temperature"] is None
    assert len(conf["examples_included"]) == (outcome.variant_by_id[conf["variant"]["id"]].shots or 0)
    assert conf["validation"]["confirmed"] in (True, False) and conf["prices"]["source"]
    text = " ".join(p.read_text(encoding="utf-8") for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert SECRET not in text and SIMULATION_BANNER not in text      # no key, and not mislabeled as a simulation


# --- user errors and confirmation ---------------------------------------------------------------

def test_missing_price_blocks_the_launch(tmp_path, capsys):
    prices = rewrite(tmp_path, "prices.yaml", "p.yaml", lambda d: d["models"].pop("modele-exemple-compatible"))
    assert main(example_args(tmp_path, "--dry-run", prices=prices)) == 2
    err = capsys.readouterr().err
    assert "modele-exemple-compatible" in err and "zéro" in err
    assert not (tmp_path / "out" / "runs").exists()


def test_current_model_must_be_a_tested_model(tmp_path, capsys):
    cfg = rewrite(tmp_path, "config.example.yaml", "c.yaml", lambda d: d.update(current_model="autre-modele"))
    assert main(example_args(tmp_path, "--dry-run", config=cfg)) == 2
    assert "current_model" in capsys.readouterr().err


def test_max_calls_is_enforced_before_any_call(tmp_path, capsys):
    cfg = rewrite(tmp_path, "config.example.yaml", "c.yaml", lambda d: d.update(max_calls=100))
    assert main(example_args(tmp_path, "--dry-run", config=cfg)) == 2
    assert "max_calls" in capsys.readouterr().err and not (tmp_path / "out" / "runs").exists()


def test_missing_api_key_stops_before_the_prompt(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SAVERUN_ANTHROPIC_KEY", raising=False)
    monkeypatch.delenv("EXEMPLE_API_KEY", raising=False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not ask before checking the keys"))
    assert main(example_args(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "SAVERUN_ANTHROPIC_KEY" in err and "EXEMPLE_API_KEY" in err


def test_declining_the_confirmation_makes_no_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SAVERUN_ANTHROPIC_KEY", "x")
    monkeypatch.setenv("EXEMPLE_API_KEY", "x")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(example_args(tmp_path)) == 1
    assert not (tmp_path / "out" / "runs").exists()


def test_cli_without_command_prints_help(capsys):
    assert main([]) == 2 and "run" in capsys.readouterr().out


# --- economy -----------------------------------------------------------------------------------

def row(variant, cost, k, n, model="m", **over):
    score, lo, hi = variant_score([1] * k + [0] * (n - k))
    fields = dict(model=model, variant=variant, description=variant, n_cases=n, n_planned=n, score=score, lo=lo,
                  hi=hi, avg_in=100, avg_out=5, cost_per_1000=cost, estimated=False, failed_share=0.0,
                  truncated_share=0.0)
    return Row(**{**fields, **over})


def cfg(**over):
    models = [{"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10}]
    return parse_config({"intention": "x", "models": models, "base_prompt": "actuel", "current_model": "m",
                         "monthly_volume": 50_000, **over})


def test_economy_by_hand_at_equal_quality():
    sel = select([row("BASE", 0.80, 95, 100), row("V01", 0.20, 95, 100)], 0.75)
    eco = compute_economy(sel, cfg())
    assert eco.reason is None and eco.equal_quality is True
    assert eco.savings.per_1000 == pytest.approx(0.60) and eco.savings.percent == pytest.approx(0.75)
    assert eco.savings.monthly == pytest.approx(30.0)


def test_economy_is_not_at_equal_quality_when_base_misses_the_threshold():
    sel = select([row("BASE", 0.80, 50, 100), row("V01", 0.20, 95, 100)], 0.75)
    eco = compute_economy(sel, cfg())
    assert eco.savings is not None and eco.equal_quality is False


def bare_config(**over):
    """A config with neither base_prompt nor current_model unless given."""
    models = [{"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10}]
    return parse_config({"intention": "x", "models": models, **over})


@pytest.mark.parametrize("config,fragment", [
    (bare_config(current_model="m"), "base_prompt"),
    (bare_config(base_prompt="actuel"), "current_model"),
    (bare_config(), "base_prompt"),
])
def test_no_assumption_when_the_reference_is_unknown(config, fragment):
    eco = compute_economy(select([row("BASE", 0.8, 95, 100), row("V01", 0.2, 95, 100)], 0.75), config)
    assert eco.savings is None and eco.reference is None and fragment in eco.reason


def test_economy_without_a_measurable_reference_or_winner():
    unmeasured = select([row("V01", 0.2, 95, 100)], 0.75)           # no BASE row at all
    assert compute_economy(unmeasured, cfg()).savings is None
    no_winner = select([row("BASE", 0.8, 50, 100), row("V01", 0.2, 50, 100)], 0.75)
    eco = compute_economy(no_winner, cfg())
    assert eco.savings is None and eco.reference is not None and "aucune variante" in eco.reason


def test_winner_can_be_the_current_setup_or_dearer():
    same = compute_economy(select([row("BASE", 0.2, 95, 100), row("V01", 0.5, 95, 100)], 0.75), cfg())
    assert same.winner_is_reference and same.savings.per_1000 == 0
    dearer = compute_economy(select([row("BASE", 0.2, 50, 100), row("V01", 0.5, 95, 100)], 0.75), cfg())
    assert dearer.savings.per_1000 < 0


# --- wording -----------------------------------------------------------------------------------

def outcome(rows, validations=(), **cfg_over):
    config = cfg(**cfg_over)
    sel = select(rows, 0.75)
    return NS(economy=compute_economy(sel, config), selection=sel, validations=list(validations),
              plan=NS(config=config, prices=NS(currency="EUR")))


def val(confirmed, winner=True, n=7):
    return Validation("m", "V01", n, n, 0.9 if confirmed else 0.5, 0.4, 0.99, confirmed, n < 20, winner)


def test_verdict_variants():
    rows = [row("BASE", 0.8, 95, 100), row("V01", 0.2, 95, 100)]
    assert "confirmée sur les cas de validation" in verdict(outcome(rows, [val(True)]))
    text = verdict(outcome(rows, [val(False)]))
    assert "NON CONFIRMÉE" in text and "ajoutez des cas" in text
    assert "baissez le seuil ou ajoutez des cas" in verdict(outcome([row("V01", 0.2, 5, 10)]))


def test_unstable_winner_is_flagged_next_to_the_recommendation():
    text = verdict(outcome([row("V01", 0.2, 95, 100, failed_share=0.25)], [val(True)]))
    assert "non comparable équitablement" in text.lower() and "25 %" in text and "à relancer" in text


def test_economy_wording_never_invents_a_figure():
    lines = " ".join(economy_lines(outcome([row("V01", 0.2, 95, 100)], base_prompt=None)))
    assert "Aucun chiffre d'économie" in lines and "base_prompt" in lines and "par 1 000 appels (" not in lines
    both = economy_lines(outcome([row("BASE", 0.8, 50, 100), row("V01", 0.2, 95, 100)]))
    assert any("n'est pas « à qualité égale »" in x and "note 50 %" in x for x in both)
    assert any("30,00 EUR par mois" in x for x in economy_lines(outcome(
        [row("BASE", 0.8, 95, 100), row("V01", 0.2, 95, 100)])))


def test_validation_wording():
    o = outcome([row("V01", 0.2, 95, 100)], [val(False)])
    text = " ".join(validation_lines(o))
    assert "Indicatif" in text and "Non confirmée" in text and "re-sélectionner" in text
    assert "Aucune validation" in validation_lines(outcome([row("V01", 0.2, 3, 10)]))[0]


def test_readme_example_is_the_real_output_of_the_example_files(tmp_path):
    """The README example must not drift from what `saverun run --dry-run` actually produces."""
    main(example_args(tmp_path, "--dry-run"))
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    verdict_line = report.split("## Verdict")[1].split("\n\n")[1].strip()
    readme = open("README.md", encoding="utf-8").read()
    assert verdict_line in readme and SIMULATION_BANNER in readme
    rows = {(r["variant"], r["model"]): r for r in csv.DictReader((tmp_path / "out" / "results.csv").open(encoding="utf-8"))}
    for key in [("V01", "modele-exemple-compatible"), ("V05", "modele-exemple-compatible"),
                ("V08", "modele-exemple-anthropic"), ("V20", "modele-exemple-anthropic"),
                ("BASE", "modele-exemple-anthropic")]:
        r = rows[key]
        cost = f"{float(r['cost_per_1000']):.3f}".replace(".", ",")
        assert f"| {cost}" in readme or f"{cost} \*" in readme, key
    assert rows[("V08", "modele-exemple-anthropic")]["status"] == "retenue"
    assert rows[("BASE", "modele-exemple-anthropic")]["status"] == "seuil démontré, plus chère"


def test_prepare_puts_both_values_in_every_few_shot_variant(tmp_path):
    """urgent / not urgent: examples are balanced on the scored field, whatever the seed and the free-text field."""
    from saverun.cost import ModelPrice
    from saverun.splitting import Case
    cases = [Case(f"C{i:03d}", f"mail {i}", json.dumps({"urgent": i % 5 == 0, "note": f"n{i}"})) for i in range(1, 41)]
    model = {"id": "m", "provider": "anthropic", "api_key_env": "K", "rpm": 10}
    prices = PriceTable("EUR", {"m": ModelPrice(1.0, 5.0, "d", "s")})
    for seed in range(8):
        config = parse_config({"intention": "x", "models": [model], "scoring": "json_fields", "fields": ["urgent"],
                               "output_format": "Réponds en JSON.", "n_variants": 12, "seed": seed})
        plan = prepare(config, cases, prices, RunStore(tmp_path / f"r{seed}"), dry_run=True)
        assert {json.loads(c.expected)["urgent"] for c in plan.split.examples} == {True, False}
        shots = [v for v in plan.variants if v.shots == 3]
        assert shots and all('"urgent": true' in v.system and '"urgent": false' in v.system for v in shots)
        assert not [w for w in plan.warnings if "valeurs attendues" in w]
