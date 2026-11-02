import pytest

from saverun.splitting import CasesError, Case, load_cases, split_cases


def make(n):
    return [Case(f"C{i:03d}", f"in{i}", "x") for i in range(1, n + 1)]


def ids(cases):
    return {c.id for c in cases}


@pytest.mark.parametrize("n", [9, 20, 50, 137])
def test_no_overlap_and_nothing_lost(n):
    s = split_cases(make(n), 0.25, seed=1)
    a, b, c = ids(s.examples), ids(s.selection), ids(s.validation)
    assert not (a & b) and not (a & c) and not (b & c)
    assert len(a) + len(b) + len(c) == n
    assert len(a) == 3


def test_validation_part_and_determinism():
    s = split_cases(make(50), 0.25, seed=7)
    assert len(s.validation) == round(47 * 0.25) == 12 and len(s.selection) == 35
    again = split_cases(make(50), 0.25, seed=7)
    assert s == again
    assert ids(split_cases(make(50), 0.25, seed=8).validation) != ids(s.validation)


def test_small_files_warn_or_fail():
    assert any("indicatif" in w for w in split_cases(make(19), 0.25, 0).warnings)
    assert not split_cases(make(20), 0.25, 0).warnings
    with pytest.raises(CasesError):
        split_cases(make(6), 0.25, 0)


def test_load_cases(tmp_path):
    f = tmp_path / "cases.csv"
    f.write_text("input,expected\nA,x\nB,y\n\nA,x\nB,z\n", encoding="utf-8")
    cases, warnings = load_cases(f)
    assert [c.input for c in cases] == ["A", "B"]
    assert cases[0].id == "C001" and len(warnings) == 2  # one duplicate summary + one conflicting expected


def test_load_cases_errors(tmp_path):
    with pytest.raises(CasesError):
        load_cases(tmp_path / "absent.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("texte,reponse\nA,x\n", encoding="utf-8")
    with pytest.raises(CasesError):
        load_cases(bad)
    half = tmp_path / "half.csv"
    half.write_text("input,expected\nA,\n", encoding="utf-8")
    with pytest.raises(CasesError):
        load_cases(half)


def test_example_file_loads():
    cases, warnings = load_cases("cases.example.csv")
    assert len(cases) >= 20 and not warnings
