"""Prices and costs. No price is ever invented: a model missing from prices.yaml blocks the run."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import yaml

UNVERIFIED_MARK = "À VÉRIFIER"


class PriceError(ValueError):
    """Unusable price file. User-facing (French)."""


class MissingPriceError(PriceError):
    """One or more models have no price. Never valued at zero."""

    def __init__(self, models: list[str], path: str | Path, unset: list[str] | None = None):
        self.models = models + (unset or [])
        parts = []
        if models:
            parts.append(f"Modèle(s) absent(s) de {path} : {', '.join(models)}. "
                         "Ajoutez leur prix (avec date et source) : aucun n'est valorisé à zéro.")
        if unset:
            parts.append(f"Prix non renseignés (« {UNVERIFIED_MARK} ») dans {path} pour : {', '.join(unset)}. "
                         "Remplissez input_per_million et output_per_million depuis la page officielle du "
                         "fournisseur, avec la date et la source : aucun prix n'est inventé ni valorisé à zéro.")
        super().__init__(" ".join(parts))


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    date: str
    source: str
    free: bool = False  # free tier: the rates are an explicit reference tariff, costs are "estimated"

    @property
    def unverified(self) -> bool:
        return UNVERIFIED_MARK in self.date or UNVERIFIED_MARK in self.source


@dataclass(frozen=True)
class PriceTable:
    currency: str
    models: dict[str, ModelPrice]
    path: str = "prices.yaml"
    unset: frozenset[str] = frozenset()  # listed, but rates still « À VÉRIFIER »: they block any run using them

    def get(self, model_id: str) -> ModelPrice | None:
        return self.models.get(model_id)

    def require(self, model_ids: list[str]) -> None:
        """Raise MissingPriceError listing every model without a usable price (absent or not yet filled in)."""
        missing = [m for m in model_ids if m not in self.models and m not in self.unset]
        unset = [m for m in model_ids if m in self.unset]
        if missing or unset:
            raise MissingPriceError(missing, self.path, unset)

    def warnings(self, model_ids: list[str]) -> list[str]:
        """Notices for the models actually used: unverified prices and reference tariffs."""
        out = []
        unverified = [m for m in model_ids if m in self.models and self.models[m].unverified]
        if unverified:
            out.append(f"Prix marqués « {UNVERIFIED_MARK} » pour : {', '.join(unverified)}. "
                       "Les coûts et l'économie affichés ne sont pas fiables tant que les tarifs ne sont pas vérifiés.")
        free = [m for m in model_ids if m in self.models and self.models[m].free]
        if free:
            out.append(f"Modèle(s) gratuit(s) valorisé(s) au tarif de référence ({', '.join(free)}) : "
                       "coût estimé, pas une dépense réelle.")
        return out


def _price(model_id: str, raw: object) -> ModelPrice | None:
    """The model's price, or None when both rates are still the placeholder « À VÉRIFIER »."""
    where = f"prices.yaml, modèle {model_id}"
    if not isinstance(raw, dict):
        raise PriceError(f"{where} : doit être un dictionnaire.")
    placeholders = [str(raw.get(k, "")).strip() == UNVERIFIED_MARK for k in ("input_per_million", "output_per_million")]
    if any(placeholders) and not all(placeholders):
        raise PriceError(f"{where} : renseignez les deux tarifs, ou laissez les deux à « {UNVERIFIED_MARK} ».")
    rates = []
    for key in ("input_per_million", "output_per_million"):
        v = raw.get(key)
        if all(placeholders):
            rates.append(0.0)  # never used: the model goes to PriceTable.unset
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise PriceError(f"{where} : {key} doit être un nombre positif ou nul "
                             "(pour un modèle gratuit, saisissez un tarif de référence).")
        rates.append(float(v))
    for key in ("date", "source"):
        if not str(raw.get(key) or "").strip():
            raise PriceError(f"{where} : « {key} » est obligatoire (écrivez « {UNVERIFIED_MARK} » si inconnu).")
    free = raw.get("free", False)
    if not isinstance(free, bool):
        raise PriceError(f"{where} : free doit valoir true ou false.")
    if all(placeholders):
        return None
    return ModelPrice(rates[0], rates[1], str(raw["date"]).strip(), str(raw["source"]).strip(), free)


def load_prices(path: str | Path) -> PriceTable:
    p = Path(path)
    if not p.is_file():
        raise PriceError(f"Fichier de prix introuvable : {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise PriceError(f"YAML invalide dans {p} : {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict) or not data["models"]:
        raise PriceError(f"{p} : section « models » manquante ou vide.")
    currency = str(data.get("currency") or "").strip()
    if not currency:
        raise PriceError(f"{p} : « currency » est obligatoire (aucune conversion de monnaie n'est faite).")
    parsed = {str(k): _price(str(k), v) for k, v in data["models"].items()}
    return PriceTable(currency, {k: v for k, v in parsed.items() if v is not None}, str(p),
                      frozenset(k for k, v in parsed.items() if v is None))


# --- Costs ------------------------------------------------------------------------

def call_cost(tokens_in: float, tokens_out: float, price: ModelPrice) -> float:
    return (tokens_in * price.input_per_million + tokens_out * price.output_per_million) / 1_000_000


def cost_per_1000(avg_in: float, avg_out: float, price: ModelPrice) -> float:
    """Cost of 1 000 calls from the mean tokens per call, in the price file's currency."""
    return call_cost(avg_in, avg_out, price) * 1000


def monthly_cost(per_1000: float, monthly_volume: float | None) -> float | None:
    return None if monthly_volume is None else per_1000 * monthly_volume / 1000


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token), for pre-launch estimates only."""
    return math.ceil(len(text) / 4)


@dataclass(frozen=True)
class Savings:
    per_1000: float
    percent: float            # 0.0 to 1.0 ; negative if the best option costs more than the reference
    monthly: float | None


def savings(reference_per_1000: float, best_per_1000: float, monthly_volume: float | None = None) -> Savings:
    if reference_per_1000 <= 0:
        raise ValueError("Le coût de référence doit être strictement positif.")
    diff = reference_per_1000 - best_per_1000
    return Savings(diff, diff / reference_per_1000, monthly_cost(diff, monthly_volume))
