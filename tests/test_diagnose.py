import csv

import pytest

from saverun.cli import main
from saverun.cost import ModelPrice, PriceTable, load_prices
from saverun.diagnose import DEFAULT_INTEREST_THRESHOLD, DiagnoseError, diagnose

PRICES = PriceTable("USD", {"a": ModelPrice(1.0, 5.0, "2026-01-01", "s"),
                            "gratuit": ModelPrice(2.0, 10.0, "2026-01-01", "s", free=True)}, "prices.yaml")


def calls(tmp_path, rows, name="calls.csv"):
    f = tmp_path / name
    with f.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["date", "tache", "modele", "tokens_entree", "tokens_sortie"])
        w.writerows(rows)
    return f


def test_monthly_projection_by_hand(tmp_path):
    # 100 calls of (1000 in, 100 out) at 1.0 / 5.0 per million = 0.0015 each -> 0.15 over 10 days -> x3 = 0.45 / month
    rows = [(f"2026-10-{1 + i % 10:02d}", "tri", "a", 1000, 100) for i in range(100)]
    d = diagnose(calls(tmp_path, rows), PRICES)
    assert (d.days, d.n_calls) == (10, 100)
    assert d.monthly_total == pytest.approx(0.45) and d.rows[0].cost_per_month == pytest.approx(0.45)
    assert d.rows[0].calls_per_month == pytest.approx(300)
    assert d.currency == "USD"


def test_tasks_ranked_with_cumulative_share_and_coverage_rule(tmp_path):
    rows = ([("2026-10-01", "gros", "a", 100_000, 0)] * 8 + [("2026-10-20", "moyen", "a", 100_000, 0)] * 2
            + [("2026-10-30", "petit", "a", 1_000, 0)])
    d = diagnose(calls(tmp_path, rows), PRICES, coverage=0.80)
    assert [r.task for r in d.rows] == ["gros", "moyen", "petit"]
    assert sum(r.share for r in d.rows) == pytest.approx(1.0) and d.rows[-1].cumulative == pytest.approx(1.0)
    assert d.rows[0].share == pytest.approx(0.7992, abs=1e-4)   # just under 80 %: the next task is kept too
    assert d.selected == ["gros", "moyen"] and d.rows[2].to_optimize is False
    assert diagnose(calls(tmp_path, rows, "c2.csv"), PRICES, coverage=0.5).selected == ["gros"]


def test_unknown_model_is_counted_apart_never_valued_at_zero(tmp_path):
    rows = [("2026-10-01", "t", "a", 1000, 100)] * 4 + [("2026-10-02", "t", "inconnu", 1000, 100)] * 3
    d = diagnose(calls(tmp_path, rows), PRICES)
    assert d.unpriced == {"inconnu": 3} and d.rows[0].unpriced_calls == 3
    assert any("inconnu (3 appels)" in w and "sous-estimé" in w for w in d.warnings)


def test_unreadable_rows_short_history_and_free_model_are_reported(tmp_path):
    rows = [("2026-10-01", "t", "gratuit", 1000, 100), ("pas-une-date", "t", "a", 1, 1),
            ("2026-10-02", "t", "a", "x", 1), ("2026-10-02", "", "a", 1, 1)]
    d = diagnose(calls(tmp_path, rows), PRICES)
    assert d.ignored_rows == 3
    text = " ".join(d.warnings)
    assert "3 ligne(s) illisible(s)" in text and "Moins de 7 jours" in text and "coût estimé" in text


def test_errors(tmp_path):
    with pytest.raises(DiagnoseError):
        diagnose(tmp_path / "absent.csv", PRICES)
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(DiagnoseError, match="colonnes"):
        diagnose(bad, PRICES)
    with pytest.raises(DiagnoseError, match="exploitable"):
        diagnose(calls(tmp_path, [("nope", "t", "a", 1, 1)], "x.csv"), PRICES)


def test_interest_threshold_is_in_the_price_currency_and_is_an_arbitrary_default(tmp_path):
    d = diagnose(calls(tmp_path, [("2026-10-01", "t", "a", 1000, 100)]), PRICES, interest_threshold=0.0001)
    assert d.worth_optimizing and d.currency == "USD"
    assert DEFAULT_INTEREST_THRESHOLD == 100.0
    assert not diagnose(calls(tmp_path, [("2026-10-01", "t", "a", 1000, 100)], "y.csv"), PRICES).worth_optimizing


def test_example_export_with_the_example_prices(tmp_path, capsys):
    d = diagnose("prototypes/calls.example.csv", load_prices("prices.yaml"))
    assert d.rows[0].cost_per_month >= d.rows[-1].cost_per_month
    assert {r.task for r in d.rows} >= {"classement_tickets", "brouillon_reponses"}
    assert d.ignored_rows == 1 and d.unpriced == {"modele_inconnu": 12}   # fixtures of the example file
    out = tmp_path / "d.csv"
    assert main(["diagnose", "prototypes/calls.example.csv", "--prices", "prices.yaml", "--out", str(out)]) == 0
    shown = capsys.readouterr().out
    assert "EUR" in shown and "€" not in shown and "arbitraire" in shown      # currency from the price file
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0]["currency"] == "EUR" and len(rows) == len(d.rows)


def test_currency_follows_the_price_file(tmp_path, capsys):
    prices = tmp_path / "p.yaml"
    prices.write_text("currency: CHF\nmodels:\n  a: {input_per_million: 1, output_per_million: 5, date: d, source: s}\n",
                      encoding="utf-8")
    f = calls(tmp_path, [("2026-10-01", "t", "a", 1000, 100)])
    assert main(["diagnose", str(f), "--prices", str(prices), "--out", str(tmp_path / "o.csv")]) == 0
    shown = capsys.readouterr().out
    assert "CHF" in shown and "EUR" not in shown
