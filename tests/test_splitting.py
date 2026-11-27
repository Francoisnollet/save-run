import pytest

from saverun.splitting import MIN_CASES_PER_TAG, UNTAGGED, CasesError, Case, load_cases, split_cases, tag_warnings


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


# --- optional tag column --------------------------------------------------------------------------

def write_tagged(tmp_path, rows, header="input,expected,tag"):
    path = tmp_path / "cases.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_two_column_files_have_no_tag_and_no_warning(tmp_path):
    cases, warnings = load_cases(write_tagged(tmp_path, ["a,oui", "b,non"], header="input,expected"))
    assert [c.tag for c in cases] == ["", ""] and not warnings


def test_tag_is_read_and_untagged_cases_are_flagged(tmp_path):
    cases, warnings = load_cases(write_tagged(tmp_path, ["a,oui,F1", "b,non,"]))
    assert [c.tag for c in cases] == ["F1", ""]
    assert any("1 cas sans tag" in w and UNTAGGED in w for w in warnings)


def test_all_cases_tagged_gives_no_warning(tmp_path):
    assert load_cases(write_tagged(tmp_path, ["a,oui,F1", "b,non,F2"]))[1] == []


def test_same_input_with_another_tag_is_flagged(tmp_path):
    cases, warnings = load_cases(write_tagged(tmp_path, ["a,oui,F1", "a,oui,F2"]))
    assert len(cases) == 1 and any("autre tag" in w for w in warnings)


def test_tag_does_not_change_the_split():
    plain = [Case(f"C{i:03d}", f"e{i}", "oui" if i % 2 else "non") for i in range(40)]
    tagged = [Case(c.id, c.input, c.expected, tag=f"F{i % 3}") for i, c in enumerate(plain)]
    a, b = split_cases(plain, 0.25, 0), split_cases(tagged, 0.25, 0)
    for part in ("examples", "selection", "validation"):
        assert [c.id for c in getattr(a, part)] == [c.id for c in getattr(b, part)]


def test_warning_when_a_tag_has_too_few_selection_cases():
    enough = [Case(f"A{i}", f"a{i}", "x", tag="A") for i in range(MIN_CASES_PER_TAG)]
    few = [Case(f"B{i}", f"b{i}", "x", tag="B") for i in range(MIN_CASES_PER_TAG - 1)]
    untagged = [Case(f"C{i}", f"c{i}", "x") for i in range(2)]
    warnings = tag_warnings(enough + few + untagged)
    assert len(warnings) == 1 and f"B ({MIN_CASES_PER_TAG - 1})" in warnings[0] and "A (" not in warnings[0]
    assert tag_warnings(enough) == []


# --- optional critical column -------------------------------------------------------------------------

import csv

from saverun.splitting import CRITICAL_FALSE, CRITICAL_TRUE


def test_without_the_critical_column_no_case_is_critical(tmp_path):
    cases, warnings = load_cases(write_tagged(tmp_path, ["a,oui,F1", "b,non,F2"]))
    assert [c.critical for c in cases] == [False, False] and warnings == []


@pytest.mark.parametrize("value", [*CRITICAL_TRUE, " OUI ", "True", "Yes"])
def test_critical_true_values(tmp_path, value):
    cases, _ = load_cases(write_tagged(tmp_path, [f"a,x,{value}"], header="input,expected,critical"))
    assert cases[0].critical is True


@pytest.mark.parametrize("value", [*CRITICAL_FALSE, " Non ", "FALSE"])
def test_critical_false_values_and_empty_cell(tmp_path, value):
    cases, _ = load_cases(write_tagged(tmp_path, ["a,x,oui", f"b,x,{value}"], header="input,expected,critical"))
    assert [c.critical for c in cases] == [True, False]


@pytest.mark.parametrize("value", ["peut-être", "o", "2", "vrai"])
def test_an_invalid_critical_value_stops_with_its_line(tmp_path, value):
    path = write_tagged(tmp_path, ["a,x,oui", f"b,x,{value}"], header="input,expected,critical")
    with pytest.raises(CasesError, match=f"Ligne 3 .*« {value} ».*critical"):
        load_cases(path)


def test_critical_column_with_no_critical_case_is_flagged(tmp_path):
    _, warnings = load_cases(write_tagged(tmp_path, ["a,x,non", "b,x,"], header="input,expected,critical"))
    assert len(warnings) == 1 and "aucun cas n'est marqué critique" in warnings[0]


@pytest.mark.parametrize("first,second", [("oui", "non"), ("non", "oui")])
def test_a_duplicate_stays_critical_if_either_line_is(tmp_path, first, second):
    cases, warnings = load_cases(write_tagged(tmp_path, [f"a,x,F1,{first}", "b,x,F1,non", f"a,x,F1,{second}"],
                                              header="input,expected,tag,critical"))
    assert [(c.id, c.critical) for c in cases] == [("C001", True), ("C002", False)]   # first line's id is kept
    (w,) = [w for w in warnings if "autre valeur « critical »" in w]
    assert w == ("Ligne 4 : même input que la ligne précédente avec une autre valeur « critical » ; le cas est "
                 "critique, car l'une des deux lignes l'est. Vérifiez vos données.")


def test_same_input_and_same_critical_value_is_a_plain_duplicate(tmp_path):
    cases, warnings = load_cases(write_tagged(tmp_path, ["a,x,non", "a,x,"], header="input,expected,critical"))
    assert [c.critical for c in cases] == [False] and not any("autre valeur « critical »" in w for w in warnings)


@pytest.mark.parametrize("name", ["Critical", " critical", "CRITIQUE", "critique "])
def test_a_lookalike_critical_header_is_ignored_with_a_warning(tmp_path, name):
    path = tmp_path / "cases.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([["input", "expected", name], ["a", "x", "oui"], ["b", "x", "non"]])
    cases, warnings = load_cases(path)
    assert [c.critical for c in cases] == [False, False]                              # not accepted silently
    assert f"Colonne « {name} » ignorée, vouliez-vous « critical » ?" in warnings


def test_critical_changes_neither_the_split_nor_the_calls(tmp_path):
    from saverun.config import load_config
    from saverun.cost import load_prices
    from saverun.pipeline import prepare
    from saverun.runner import RunStore

    plain, _ = load_cases("cases.example.csv")
    flagged = [Case(c.id, c.input, c.expected, c.tag, critical=i % 3 == 0) for i, c in enumerate(plain)]
    a, b = split_cases(plain, 0.25, 0), split_cases(flagged, 0.25, 0)
    for part in ("examples", "selection", "validation"):
        assert [c.id for c in getattr(a, part)] == [c.id for c in getattr(b, part)]
    cfg, prices = load_config("config.example.yaml"), load_prices("prices.yaml")
    pa = prepare(cfg, plain, prices, RunStore(tmp_path / "a"), dry_run=True)
    pb = prepare(cfg, flagged, prices, RunStore(tmp_path / "b"), dry_run=True)
    assert [j.key for j in pa.jobs] == [j.key for j in pb.jobs]
