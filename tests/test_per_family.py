"""tools/per_family.py: scores per family, from the calls of a run. Fake data and fake model only."""
import csv
import dataclasses
import importlib.util
import sys
from pathlib import Path

from saverun.config import load_config
from saverun.cost import load_prices
from saverun.fake import FakeClient
from saverun.pipeline import execute_plan, prepare
from saverun.report import write_outputs
from saverun.runner import CallResult, ModelRuntime, RunStore, _sha
from saverun.scoring import wilson
from saverun.splitting import Case

spec = importlib.util.spec_from_file_location("per_family", Path(__file__).parent.parent / "tools" / "per_family.py")
per_family = importlib.util.module_from_spec(spec)
sys.modules["per_family"] = per_family  # its dataclasses look it up there
spec.loader.exec_module(per_family)

YES, NO = '{"urgent": true}', '{"urgent": false}'


def write_families(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["texte", "urgent", "famille"])
        w.writerows(rows)
    return path


def call(text, output=YES, model="m", variant="V01", phase="selection", status="ok", prompt="p", run=0):
    return CallResult(model=model, variant=variant, prompt_hash=prompt, case="C" + _sha(text)[:4], input_hash=_sha(text),
                      run=run, phase=phase, temperature=None, max_tokens=500, status=status,
                      output=output if status == "ok" else None)


def make_run(tmp_path, calls):
    store = RunStore(tmp_path / "run" / "runs")
    for c in calls:
        store.append(c)
    return tmp_path / "run"


def emails(*rows):
    return {_sha(t): per_family.Email(t, fam, urgent) for t, fam, urgent in rows}


TWO_FAMILIES = (("a", "F1", True), ("b", "F1", True), ("c", "F2", False))


def test_scores_are_per_family_with_the_wilson_interval():
    a = per_family.analyse([call("a"), call("b", NO), call("c", NO)], emails(*TWO_FAMILIES))
    assert a.families == ["F1", "F2"]
    per_email = a.rates[("m", "V01")]
    assert per_family.family_score(per_email, a.emails, "F1") == (1, 2)
    assert per_family.family_score(per_email, a.emails, "F2") == (1, 1)
    lo, hi = wilson(1, 2)
    assert per_family._score_cell(1, 2) == f"1/2 : 50 % ({per_family.pct(lo)} – {per_family.pct(hi)})"
    assert per_family._score_cell(0, 0) == "—"


def test_only_selection_calls_count_and_failures_are_excluded():
    calls = [call("a"), call("b", NO, phase="validation"), call("c", status="failed")]
    a = per_family.analyse(calls, emails(*TWO_FAMILIES))
    assert list(a.rates[("m", "V01")]) == [_sha("a")]


def test_several_runs_of_a_case_give_a_fractional_score():
    a = per_family.analyse([call("a", run=0), call("a", NO, run=1)], emails(*TWO_FAMILIES))
    assert a.rates[("m", "V01")][_sha("a")] == 0.5
    assert per_family._score_cell(0.5, 1).startswith("0.5/1 : 50 %")


def test_the_score_uses_the_answer_after_the_reasoning_marker():
    a = per_family.analyse([call("a", 'Peut-être {"urgent": false}\nRéponse : {"urgent": true}')], emails(*TWO_FAMILIES))
    assert a.rates[("m", "V01")][_sha("a")] == 1


def test_an_email_missing_from_the_families_file_is_flagged_not_counted():
    a = per_family.analyse([call("a"), call("inconnu")], emails(*TWO_FAMILIES))
    assert list(a.rates[("m", "V01")]) == [_sha("a")]
    assert "1 e-mail(s)" in a.warnings[0]


def test_an_older_prompt_under_the_same_variant_name_is_not_counted():
    a = per_family.analyse([call("a", NO, prompt="ancien"), call("a", prompt="nouveau")], emails(*TWO_FAMILIES))
    assert a.rates[("m", "V01")][_sha("a")] == 1 and "plusieurs prompts" in a.warnings[0]


def test_hardest_emails_come_first_with_their_family():
    calls = [call("a", NO, variant="V01"), call("a", NO, variant="V02"), call("b", NO, variant="V01"),
             call("b", variant="V02"), call("c", NO, variant="V01"), call("c", NO, variant="V02")]
    missed = per_family.hardest(per_family.analyse(calls, emails(*TWO_FAMILIES)))
    assert [(m, n, e.text, e.family) for m, n, e in missed] == [(2, 2, "a", "F1"), (1, 2, "b", "F1")]


def test_report_escapes_cells_and_cuts_the_excerpt(tmp_path):
    long = "Sujet : a|b\nContenu : " + "x" * 200
    a = per_family.analyse([call(long, NO)], emails((long, "F|1", True)))
    text = per_family.render(a, "f.csv", 10, 0)
    assert "F\\|1" in text and "Sujet : a\\|b Contenu" in text and "x" * 150 not in text and "…" in text
    header = next(line for line in text.splitlines() if line.startswith("| Variante"))
    assert header.count(" | ") == 2                         # variant, model, one family: the escaped | is not a column


def test_report_says_so_when_nothing_is_missed():
    a = per_family.analyse([call("a")], emails(*TWO_FAMILIES))
    assert "Aucun : tous les couples" in per_family.render(a, "f.csv", 10, 1)


def test_top_limits_the_list_and_counts_the_rest():
    a = per_family.analyse([call("a", NO), call("b", NO)], emails(*TWO_FAMILIES))
    text = per_family.render(a, "f.csv", 1, 1)
    assert "… et 1 autre(s)" in text


def test_a_total_that_differs_from_results_csv_is_flagged(tmp_path):
    results = tmp_path / "results.csv"
    results.write_text("model,variant,score,n_cases\nm,V01,0.5,2\nm,V02,1.0,2\n", encoding="utf-8")
    a = per_family.analyse([call("a"), call("b", NO), call("a", variant="V02"), call("b", NO, variant="V02"),
                            call("a", variant="V03")], emails(*TWO_FAMILIES))
    matching, warnings = per_family.compare_with_results(results, a)
    assert matching == 1 and len(warnings) == 2
    assert any("V02" in w and "note différente" in w for w in warnings) and any("V03" in w and "absent" in w for w in warnings)


def test_main_writes_the_table_next_to_the_run(tmp_path, capsys):
    run = make_run(tmp_path, [call("a"), call("c", NO)])
    families = write_families(tmp_path / "f.csv", [("a", "oui", "F1"), ("b", "oui", "F1"), ("c", "non", "F2")])
    assert per_family.main([str(run), str(families)]) == 0
    text = (run / "par_famille.md").read_text(encoding="utf-8")
    assert "| Variante | Modèle | F1 | F2 |" in text and "| V01 | m | 1/1 : 100 %" in text
    assert "results.csv absent" in text and "results.csv absent" in capsys.readouterr().out


def test_multiline_text_with_commas_links_to_its_call(tmp_path):
    text = "Sujet : x, y\nDe : a@exemple.invalid\nContenu : « z »"
    run = make_run(tmp_path, [call(text)])
    families = write_families(tmp_path / "f.csv", [(text, "oui", "F1")])
    assert per_family.main([str(run), str(families)]) == 0
    assert "1/1" in (run / "par_famille.md").read_text(encoding="utf-8")


def test_unusable_inputs_stop_with_a_clear_message(tmp_path, capsys):
    good = write_families(tmp_path / "f.csv", [("a", "oui", "F1")])
    assert per_family.main([str(tmp_path / "absent"), str(good)]) == 2
    assert "introuvable" in capsys.readouterr().err

    run = make_run(tmp_path, [call("a")])
    assert per_family.main([str(run), str(tmp_path / "absent.csv")]) == 2
    assert "introuvable" in capsys.readouterr().err

    no_column = tmp_path / "g.csv"
    no_column.write_text("texte,urgent\na,oui\n", encoding="utf-8")
    assert per_family.main([str(run), str(no_column)]) == 2
    assert "colonnes" in capsys.readouterr().err

    bad_label = write_families(tmp_path / "h.csv", [("a", "peut-être", "F1")])
    assert per_family.main([str(run), str(bad_label)]) == 2
    assert "ligne 2" in capsys.readouterr().err

    other = write_families(tmp_path / "i.csv", [("autre", "oui", "F1")])
    assert per_family.main([str(run), str(other)]) == 2
    assert "aucun appel" in capsys.readouterr().err
    assert not (run / "par_famille.md").exists()


def test_totals_match_results_csv_after_a_full_run_on_the_fake_model(tmp_path, capsys):
    rows = [(f"E-mail fictif numéro {i}", "oui" if i % 2 else "non", f"F{i % 3}") for i in range(30)]
    families = write_families(tmp_path / "f.csv", rows)
    cases = [Case(f"C{i:03d}", t, YES if u == "oui" else NO) for i, (t, u, _) in enumerate(rows, start=1)]
    cfg = dataclasses.replace(load_config("config.example.yaml"), scoring="json_fields", fields=["urgent"],
                              n_variants=3, output_format='Réponds en JSON : {"urgent": true ou false}')
    out = tmp_path / "out"
    store = RunStore(out / "runs")
    plan = prepare(cfg, cases, load_prices("prices.yaml"), store, dry_run=False)
    runtimes = {m.id: ModelRuntime(FakeClient(m.id, {c.input: c.expected for c in cases}, seed=0), None)
                for m in cfg.models}
    write_outputs(execute_plan(plan, runtimes, store), out)

    assert per_family.main([str(out), str(families)]) == 0
    printed = capsys.readouterr().out
    n_couples = len(plan.all_variants) * len(cfg.models)
    assert f"pour {n_couples} couple(s) sur {n_couples}" in printed and "!" not in printed
    with (out / "results.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == n_couples
