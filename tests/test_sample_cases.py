"""tools/sample_cases.py: balanced, reproducible draw; the file it writes is accepted as is by SAVE Run."""
import csv
import importlib.util
from collections import Counter
from pathlib import Path

import pytest

from saverun.scoring import score_output, validate_expected
from saverun.splitting import load_cases

spec = importlib.util.spec_from_file_location("sample_cases", Path(__file__).parent.parent / "tools" / "sample_cases.py")
sample_cases = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sample_cases)


def write_source(path, rows, header=("text", "category")):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def source(tmp_path, n=10, labels=("a", "b", "c"), name="src.csv"):
    rows = [(f"message {label} {i}, with a comma", label) for label in labels for i in range(n)]
    return write_source(tmp_path / name, rows)


def run(tmp_path, src, *extra, out="cases.csv"):
    target = tmp_path / out
    code = sample_cases.main([str(src), "-o", str(target), *extra])
    return code, target


def test_round_trip_balanced_and_scored_exactly(tmp_path):
    code, out = run(tmp_path, source(tmp_path), "--labels", "a,b", "--per-label", "4")
    assert code == 0
    cases, warnings = load_cases(out)                       # read by SAVE Run's own loader
    assert not warnings and len(cases) == 8
    assert Counter(c.expected for c in cases) == {"a": 4, "b": 4}
    assert all(c.input.startswith(f"message {c.expected} ") for c in cases)
    assert out.read_text(encoding="utf-8").splitlines()[0] == "input,expected"
    validate_expected([c.expected for c in cases], "exact", None)
    assert score_output("a", "a", "exact").correct and not score_output("b", "a", "exact").correct


def test_labels_file_gives_the_tag_column(tmp_path):
    labels = tmp_path / "labels.csv"
    labels.write_text("label,tag\na,F1\nb,F2\nc,\n", encoding="utf-8")
    code, out = run(tmp_path, source(tmp_path), "--labels-file", str(labels), "--per-label", "3")
    assert code == 0
    cases, warnings = load_cases(out)
    assert {(c.expected, c.tag) for c in cases} == {("a", "F1"), ("b", "F2"), ("c", "")}
    assert any("3 cas sans tag" in w for w in warnings)


def test_same_seed_same_draw_other_seed_other_draw(tmp_path):
    src = source(tmp_path, n=30)
    args = ["--labels", "a,b,c", "--per-label", "5"]
    _, first = run(tmp_path, src, *args, "--seed", "7", out="1.csv")
    _, again = run(tmp_path, src, *args, "--seed", "7", out="2.csv")
    _, other = run(tmp_path, src, *args, "--seed", "8", out="3.csv")
    assert first.read_bytes() == again.read_bytes()
    assert {c.input for c in load_cases(first)[0]} != {c.input for c in load_cases(other)[0]}


def test_draw_ignores_row_order_and_other_labels(tmp_path):
    rows = [(f"message {label} {i}", label) for label in "abc" for i in range(20)]
    forward = write_source(tmp_path / "f.csv", rows)
    backward = write_source(tmp_path / "b.csv", rows[::-1])
    pick = lambda src, labels, out: {c.input for c in load_cases(run(tmp_path, src, "--labels", labels,
                                                                     "--per-label", "6", out=out)[1])[0]
                                     if c.expected == "a"}
    assert pick(forward, "a,b,c", "1.csv") == pick(backward, "a,b,c", "2.csv") == pick(forward, "a", "3.csv")


def test_custom_columns_and_delimiter(tmp_path):
    src = tmp_path / "s.csv"
    src.write_text("id;message;intent\n1;bonjour;x\n2;salut;x\n3;au revoir;y\n", encoding="utf-8")
    code, out = run(tmp_path, src, "--labels", "x", "--per-label", "2", "--text-column", "message",
                    "--label-column", "intent", "--delimiter", ";")
    assert code == 0 and sorted(c.input for c in load_cases(out)[0]) == ["bonjour", "salut"]


def test_duplicates_kept_once_and_ambiguous_texts_dropped(tmp_path, capsys):
    rows = [("same", "a"), ("same", "a"), ("both", "a"), ("both", "b"), ("x", "a"), ("y", "b")]
    code, out = run(tmp_path, write_source(tmp_path / "s.csv", rows), "--labels", "a,b", "--per-label", "1")
    assert code == 0
    printed = capsys.readouterr().out
    assert "1 ligne(s) en double" in printed and "1 texte(s) présent(s) sous plusieurs étiquettes" in printed
    assert "both" not in {c.input for c in load_cases(out)[0]}


@pytest.mark.parametrize("extra,fragment", [
    (["--labels", "a,zzz", "--per-label", "2"], "absente(s) du fichier : zzz"),
    (["--labels", "a", "--per-label", "11"], "Pas assez de cas pour 11 par étiquette : a (10)"),
    (["--labels", "a", "--per-label", "0"], "--per-label doit être au moins 1"),
    (["--labels", " , ", "--per-label", "1"], "Aucune étiquette"),
    (["--labels", "a", "--per-label", "1", "--text-column", "body"], "colonne(s) absente(s) : body"),
])
def test_errors_write_nothing(tmp_path, capsys, extra, fragment):
    code, out = run(tmp_path, source(tmp_path), *extra)
    assert code == 2 and not out.exists() and fragment in capsys.readouterr().err


def test_bad_labels_file(tmp_path, capsys):
    labels = tmp_path / "labels.csv"
    labels.write_text("a,F1\na,F2\n", encoding="utf-8")
    code, out = run(tmp_path, source(tmp_path), "--labels-file", str(labels), "--per-label", "1")
    assert code == 2 and not out.exists() and "en double" in capsys.readouterr().err


def test_no_silent_overwrite(tmp_path):
    src = source(tmp_path)
    out = tmp_path / "cases.csv"
    out.write_text("mes cas à moi", encoding="utf-8")
    assert run(tmp_path, src, "--labels", "a", "--per-label", "1")[0] == 2
    assert out.read_text(encoding="utf-8") == "mes cas à moi"
    assert run(tmp_path, src, "--labels", "a", "--per-label", "1", "--force")[0] == 0
