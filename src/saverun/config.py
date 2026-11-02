"""Loading and validation of config.yaml, and local .env handling."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

PROVIDERS = ("anthropic", "openai_compatible")
SCORINGS = ("exact", "json_fields")


class ConfigError(ValueError):
    """Invalid configuration. The message is user-facing (French)."""


@dataclass(frozen=True)
class ModelConfig:
    id: str
    provider: str
    api_key_env: str
    rpm: int
    base_url: str | None = None
    temperature: float | None = None  # None = not sent to the API


@dataclass(frozen=True)
class Config:
    intention: str
    models: list[ModelConfig]
    base_prompt: str | None = None
    role: str | None = None
    current_model: str | None = None  # model that runs the current prompt: the reference for the saving
    n_variants: int = 20
    runs_per_case: int = 1
    seuil_qualite: float = 0.75
    validation_part: float = 0.25
    max_calls: int | None = None
    max_tokens: int = 300  # output ceiling per call
    scoring: str = "exact"
    fields: list[str] | None = None
    monthly_volume: float | None = None
    seed: int = 0
    warnings: list[str] = field(default_factory=list)


def load_dotenv(path: str | Path = ".env") -> None:
    """Load KEY=VALUE lines from a local .env without overriding the real environment."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


def _model(raw: object, i: int) -> ModelConfig:
    where = f"models[{i}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} : un modèle doit être un dictionnaire.")
    for key in ("id", "provider", "api_key_env", "rpm"):
        if raw.get(key) in (None, ""):
            raise ConfigError(f"{where} : champ obligatoire manquant : {key}.")
    if raw["provider"] not in PROVIDERS:
        raise ConfigError(f"{where} : provider inconnu « {raw['provider']} » (attendu : {', '.join(PROVIDERS)}).")
    if raw["provider"] == "openai_compatible" and not raw.get("base_url"):
        raise ConfigError(f"{where} : base_url est obligatoire pour openai_compatible.")
    if not isinstance(raw["rpm"], int) or isinstance(raw["rpm"], bool) or raw["rpm"] <= 0:
        raise ConfigError(f"{where} : rpm doit être un entier positif.")
    temp = raw.get("temperature")
    if temp is not None and (isinstance(temp, bool) or not isinstance(temp, (int, float)) or temp < 0):
        raise ConfigError(f"{where} : temperature doit être un nombre positif ou nul.")
    return ModelConfig(id=str(raw["id"]), provider=raw["provider"], api_key_env=str(raw["api_key_env"]),
                       rpm=raw["rpm"], base_url=raw.get("base_url"),
                       temperature=None if temp is None else float(temp))


def _number(data: dict, key: str, default, lo, hi, integer=False, optional=False):
    v = data.get(key, default)
    if v is None and optional:
        return None
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and (not integer or isinstance(v, int))
    if not ok or not lo <= v <= hi:
        kind = "un entier" if integer else "un nombre"
        raise ConfigError(f"{key} doit être {kind} entre {lo} et {hi}.")
    return v


def parse_config(data: object) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("Le fichier de configuration doit contenir un dictionnaire YAML.")
    if not str(data.get("intention") or "").strip():
        raise ConfigError("Champ obligatoire manquant : intention.")
    raw_models = data.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ConfigError("Champ obligatoire manquant : models (au moins un modèle).")
    models = [_model(m, i) for i, m in enumerate(raw_models)]
    ids = [m.id for m in models]
    if len(set(ids)) != len(ids):
        raise ConfigError("Identifiants de modèle en double dans models.")
    current = str(data["current_model"]).strip() if data.get("current_model") else None
    if current and current not in ids:
        raise ConfigError(f"current_model « {current} » ne figure pas dans models "
                          f"(modèles testés : {', '.join(ids)}).")

    scoring = data.get("scoring", "exact")
    if scoring not in SCORINGS:
        raise ConfigError(f"scoring inconnu « {scoring} » (attendu : {', '.join(SCORINGS)}).")
    fields = data.get("fields")
    if fields is not None and (scoring != "json_fields" or not isinstance(fields, list) or not fields):
        raise ConfigError("fields n'a de sens qu'avec scoring: json_fields, et doit être une liste non vide.")

    cfg = Config(
        intention=str(data["intention"]).strip(),
        models=models,
        base_prompt=str(data["base_prompt"]).strip() or None if data.get("base_prompt") else None,
        role=str(data["role"]).strip() or None if data.get("role") else None,
        current_model=current,
        n_variants=_number(data, "n_variants", 20, 1, 100, integer=True),
        runs_per_case=_number(data, "runs_per_case", 1, 1, 20, integer=True),
        seuil_qualite=_number(data, "seuil_qualite", 0.75, 0.01, 0.99),
        validation_part=_number(data, "validation_part", 0.25, 0.05, 0.5),
        max_calls=_number(data, "max_calls", None, 1, 10**9, integer=True, optional=True),
        max_tokens=_number(data, "max_tokens", 300, 16, 32000, integer=True),
        scoring=scoring,
        fields=[str(f) for f in fields] if fields else None,
        monthly_volume=_number(data, "monthly_volume", None, 1, 10**12, optional=True),
        seed=_number(data, "seed", 0, 0, 2**31 - 1, integer=True),
    )
    warnings = []
    if cfg.runs_per_case > 1 and any(m.temperature == 0 for m in cfg.models):
        warnings.append("runs_per_case > 1 avec une température à 0 : les passages seront identiques, "
                        "ils ne coûtent que des appels. Laissez runs_per_case à 1.")
    return replace(cfg, warnings=warnings)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Fichier de configuration introuvable : {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML invalide dans {p} : {e}") from e
    return parse_config(data)
