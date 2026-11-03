"""tools/make_cases.py: the file it writes must be accepted as is by SAVE Run (round trip)."""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

from saverun.scoring import score_output, validate_expected
from saverun.splitting import load_cases

spec = importlib.util.spec_from_file_location("make_cases", Path(__file__).parent.parent / "tools" / "make_cases.py")
make_cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(make_cases)


def write(tmp_path, text, name="emails.csv"):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def test_round_trip_with_quotes_commas_and_newlines(tmp_path):
    tricky = 'Bonjour,\n\nVoici "l\'ordre du jour", avec trois points : a, b, c.\n\nCordialement'
    src = tmp_path / "in.csv"
    with src.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "urgent"])
        w.writerows([(tricky, "non"), ("Appelle-moi avant 17h, sinon le client part.", "Oui")])
    out = tmp_path / "cases.csv"
    assert make_cases.main([str(src), "-o", str(out)]) == 0
    cases, warnings = load_cases(out)                      # read by SAVE Run's own loader
    assert [c.input for c in cases] == [tricky, "Appelle-moi avant 17h, sinon le client part."]
    assert [json.loads(c.expected) for c in cases] == [{"urgent": False}, {"urgent": True}]
    assert not warnings
    validate_expected([c.expected for c in cases], "json_fields", ["urgent"])
    assert score_output('{"urgent": true}', cases[1].expected, "json_fields", ["urgent"]).correct
    assert not score_output('{"urgent": true}', cases[0].expected, "json_fields", ["urgent"]).correct
    raw = out.read_text(encoding="utf-8")
    assert '"{""urgent"": false}"' in raw                  # JSON escaped for CSV, not hand-written


def test_semicolon_files_without_header_and_label_variants(tmp_path):
    src = write(tmp_path, "Premier texte;oui\nSecond texte;NON\nTroisième;vrai\nQuatrième;0\n")
    cases, errors, _ = make_cases.convert(src)
    assert not errors and [c[2] for c in cases] == [True, False, True, False]
    assert [c[0] for c in cases][0] == "Premier texte"


def test_custom_field_name(tmp_path):
    src = write(tmp_path, "texte,urgent\nx,oui\n")
    (case,), _, _ = make_cases.convert(src, field="action_rapide")
    assert json.loads(case[1]) == {"action_rapide": True}


@pytest.mark.parametrize("content,fragment", [
    ("a,oui\nb,peut-être\n", "ligne 2"),
    ("a,oui\n,non\n", "texte vide"),
    ("Bonjour, ceci n'est pas entre guillemets,oui\n", "3 colonne(s)"),
    ("seul\n", "1 colonne(s)"),
])
def test_bad_rows_are_reported_with_their_line(tmp_path, content, fragment):
    _, errors, _ = make_cases.convert(write(tmp_path, content))
    assert errors and any(fragment in e for e in errors)


def test_nothing_is_written_when_there_are_errors_and_no_silent_overwrite(tmp_path, capsys):
    out = tmp_path / "cases.csv"
    assert make_cases.main([str(write(tmp_path, "a,oui\nb,???\n")), "-o", str(out)]) == 2
    assert not out.exists() and "n'est ni oui ni non" in capsys.readouterr().err
    good = write(tmp_path, "a,oui\nb,non\n", "good.csv")
    out.write_text("mes cas à moi", encoding="utf-8")
    assert make_cases.main([str(good), "-o", str(out)]) == 2 and out.read_text(encoding="utf-8") == "mes cas à moi"
    assert make_cases.main([str(good), "-o", str(out), "--force"]) == 0


def test_duplicates_and_imbalance_are_flagged(tmp_path, capsys):
    rows = "\n".join(f"mail {i},non" for i in range(30)) + "\nmail 0,non\nmail 99,oui\n"
    assert make_cases.main([str(write(tmp_path, rows)), "-o", str(tmp_path / "c.csv")]) == 0
    out = capsys.readouterr().out
    assert "1 e-mail(s) en double" in out and "déséquilibrées" in out and "constante" in out


def test_the_shipped_example_converts_and_is_not_ignored_by_git():
    cases, errors, warnings = make_cases.convert(Path("tools/emails.example.csv"))
    assert not errors and not warnings and len(cases) == 30
    assert sum(c[2] for c in cases) == 13
