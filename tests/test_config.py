import os

import pytest
import yaml

from saverun.config import ConfigError, load_config, load_dotenv, parse_config

MODEL = {"id": "m1", "provider": "anthropic", "api_key_env": "K", "rpm": 10}


def base(**over):
    return {"intention": "Classe.", "models": [MODEL], **over}


def test_defaults():
    c = parse_config(base())
    assert (c.n_variants, c.runs_per_case, c.seuil_qualite, c.validation_part) == (20, 1, 0.75, 0.25)
    assert c.models[0].temperature is None  # absent : jamais envoyé à l'API


def test_example_config_is_valid():
    c = load_config("config.example.yaml")
    assert len(c.models) == 2 and c.models[1].base_url


@pytest.mark.parametrize("data", [
    {"models": [MODEL]},                                         # intention manquante
    {"intention": "x"},                                          # pas de modèle
    base(models=[{**MODEL, "provider": "autre"}]),
    base(models=[{**MODEL, "provider": "openai_compatible"}]),   # base_url manquant
    base(models=[{k: v for k, v in MODEL.items() if k != "rpm"}]),
    base(models=[MODEL, MODEL]),                                 # doublon
    base(seuil_qualite=1.5),
    base(scoring="juge"),
    base(fields=["a"]),                                          # fields sans json_fields
])
def test_invalid(data):
    with pytest.raises(ConfigError):
        parse_config(data)


def test_runs_warning_only_when_temperature_zero():
    assert parse_config(base(runs_per_case=3)).warnings == []
    zero = base(runs_per_case=3, models=[{**MODEL, "temperature": 0}])
    assert len(parse_config(zero).warnings) == 1


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    ok = tmp_path / "ok.yaml"
    ok.write_text(yaml.safe_dump(base()), encoding="utf-8")
    assert load_config(ok).intention == "Classe."


def test_dotenv_does_not_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# c\nSR_A=1\nSR_B='deux'\nSR_C=\n", encoding="utf-8")
    monkeypatch.setenv("SR_A", "reel")
    monkeypatch.delenv("SR_B", raising=False)
    monkeypatch.delenv("SR_C", raising=False)
    load_dotenv(env)
    assert os.environ["SR_A"] == "reel" and os.environ["SR_B"] == "deux" and "SR_C" not in os.environ
    monkeypatch.delenv("SR_B")
