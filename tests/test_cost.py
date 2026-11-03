import pytest
import yaml

from saverun.config import load_config
from saverun.cost import (MissingPriceError, ModelPrice, PriceError, cost_per_1000, estimate_tokens,
                          load_prices, monthly_cost, savings)

P = ModelPrice(1.0, 5.0, "2026-01-01", "https://exemple.invalid/prix")


def write(tmp_path, data):
    f = tmp_path / "prices.yaml"
    f.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return f


def entry(**over):
    return {"input_per_million": 1.0, "output_per_million": 5.0, "date": "2026-01-01", "source": "s", **over}


def test_cost_per_1000_by_hand():
    # (300 x 1.0 + 10 x 5.0) / 1e6 x 1000 = 0.35
    assert cost_per_1000(300, 10, P) == pytest.approx(0.35)


def test_monthly_cost_and_savings():
    assert monthly_cost(0.35, 50_000) == pytest.approx(17.5)
    assert monthly_cost(0.35, None) is None
    s = savings(0.80, 0.20, 50_000)
    assert s.per_1000 == pytest.approx(0.60) and s.percent == pytest.approx(0.75)
    assert s.monthly == pytest.approx(30.0)
    assert savings(0.20, 0.80).percent < 0  # a dearer "best" is reported, not hidden
    with pytest.raises(ValueError):
        savings(0, 0.1)


def test_estimate_tokens():
    assert estimate_tokens("") == 0 and estimate_tokens("abcde") == 2


def test_missing_model_is_reported_never_zero(tmp_path):
    t = load_prices(write(tmp_path, {"currency": "EUR", "models": {"a": entry()}}))
    t.require(["a"])
    with pytest.raises(MissingPriceError) as e:
        t.require(["a", "b", "c"])
    assert e.value.models == ["b", "c"] and "b, c" in str(e.value)
    assert t.get("b") is None


def test_unverified_and_free_models_are_flagged(tmp_path):
    data = {"currency": "EUR", "models": {
        "ok": entry(),
        "ref": entry(date="À VÉRIFIER"),
        "gratuit": entry(free=True),
    }}
    t = load_prices(write(tmp_path, data))
    assert t.get("ok").unverified is False and t.get("ref").unverified is True
    assert t.warnings(["ok"]) == []
    w = " ".join(t.warnings(["ref", "gratuit"]))
    assert "À VÉRIFIER" in w and "ref" in w and "coût estimé" in w and "gratuit" in w


@pytest.mark.parametrize("data", [
    {"models": {"a": entry()}},                                          # pas de currency
    {"currency": "EUR"},                                                 # pas de models
    {"currency": "EUR", "models": {"a": entry(input_per_million=-1)}},
    {"currency": "EUR", "models": {"a": entry(output_per_million=None)}},
    {"currency": "EUR", "models": {"a": entry(date="")}},
    {"currency": "EUR", "models": {"a": entry(source=None)}},
    {"currency": "EUR", "models": {"a": entry(free="oui")}},
])
def test_invalid_price_files(tmp_path, data):
    with pytest.raises(PriceError):
        load_prices(write(tmp_path, data))


def test_free_model_needs_a_reference_tariff(tmp_path):
    bad = entry(free=True)
    del bad["input_per_million"]
    with pytest.raises(PriceError):
        load_prices(write(tmp_path, {"currency": "EUR", "models": {"a": bad}}))


def test_example_prices_cover_example_config_and_are_marked():
    t = load_prices("prices.yaml")
    cfg = load_config("config.example.yaml")
    t.require([m.id for m in cfg.models])
    assert t.currency == "EUR"
    assert all(p.unverified for p in t.models.values())  # shipped values are examples only
    assert t.get("modele-exemple-compatible").free


# --- rates left at « À VÉRIFIER »: the model is known, but any run using it is blocked ---------------------

def test_placeholder_rates_block_a_run_without_inventing_a_price(tmp_path):
    placeholder = {"input_per_million": "À VÉRIFIER", "output_per_million": "À VÉRIFIER",
                   "date": "À VÉRIFIER", "source": "À VÉRIFIER"}
    t = load_prices(write(tmp_path, {"currency": "EUR", "models": {"ok": entry(), "nouveau": placeholder}}))
    assert t.get("nouveau") is None and "nouveau" in t.unset and "ok" in t.models
    t.require(["ok"])                                          # the other models are unaffected
    with pytest.raises(MissingPriceError) as e:
        t.require(["ok", "nouveau", "absent"])
    msg = str(e.value)
    assert "nouveau" in msg and "absent" in msg and "non renseignés" in msg and "inventé" in msg
    assert set(e.value.models) == {"nouveau", "absent"}


def test_only_one_rate_left_as_placeholder_is_an_error(tmp_path):
    half = entry(output_per_million="À VÉRIFIER")
    with pytest.raises(PriceError, match="les deux tarifs"):
        load_prices(write(tmp_path, {"currency": "EUR", "models": {"a": half}}))
    junk = entry(input_per_million="cher", output_per_million="cher")
    with pytest.raises(PriceError):
        load_prices(write(tmp_path, {"currency": "EUR", "models": {"a": junk}}))


def test_shipped_prices_leave_the_first_real_test_models_unset():
    t = load_prices("prices.yaml")
    assert {"claude-sonnet-4-5-20250929", "claude-haiku-5-5"} <= t.unset
    assert not {"claude-sonnet-4-5-20250929", "claude-haiku-5-5"} & set(t.models)   # no value was invented
