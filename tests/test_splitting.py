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


# --- few-shot examples cover every expected value, when the slots allow it ------------------------------

import json

from saverun.scoring import expected_label


def labelled(counts):
    cases, i = [], 1
    for label, n in counts.items():
        for _ in range(n):
            cases.append(Case(f"C{i:03d}", f"in{i}", label))
            i += 1
    return cases


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("counts", [{"a": 13, "b": 17}, {"a": 2, "b": 38}, {"a": 1, "b": 1, "c": 28},
                                    {"a": 10, "b": 10, "c": 10}])
def test_examples_cover_every_expected_value_when_there_are_enough_slots(counts, seed):
    s = split_cases(labelled(counts), 0.25, seed)
    assert {c.expected for c in s.examples} == set(counts) and len(s.examples) == 3
    assert not any("valeurs attendues" in w for w in s.warnings)
    a, b, c = ids(s.examples), ids(s.selection), ids(s.validation)
    assert not (a & b) and not (a & c) and not (b & c) and len(a | b | c) == sum(counts.values())


def test_too_many_expected_values_warns_and_names_the_missing_ones():
    counts = {f"classe{i}": 6 for i in range(5)}
    s = split_cases(labelled(counts), 0.25, 0)
    assert len({c.expected for c in s.examples}) == 3                  # as many distinct values as slots
    (w,) = [w for w in s.warnings if "valeurs attendues" in w]
    assert "5 valeurs attendues distinctes pour 3 exemples" in w and "2 valeur(s)" in w
    assert all(missing in w for missing in set(counts) - {c.expected for c in s.examples})


def test_single_value_files_behave_as_before():
    s = split_cases(labelled({"a": 30}), 0.25, 0)
    assert len(s.examples) == 3 and not s.warnings


def test_example_order_does_not_follow_the_values():
    firsts = {split_cases(labelled({"a": 15, "b": 15}), 0.25, seed).examples[0].expected for seed in range(30)}
    assert firsts == {"a", "b"}


def test_json_fields_values_ignore_fields_that_are_not_scored():
    cases = [Case(f"C{i:03d}", f"in{i}", json.dumps({"urgent": i % 2 == 0, "note": f"libre {i}"})) for i in range(1, 41)]
    label = lambda c: expected_label(c.expected, "json_fields", ["urgent"])
    s = split_cases(cases, 0.25, 3, label=label)
    assert {json.loads(c.expected)["urgent"] for c in s.examples} == {True, False}
    assert not any("valeurs attendues" in w for w in s.warnings)
    assert any("40 valeurs attendues distinctes" in w for w in split_cases(cases, 0.25, 3).warnings)
