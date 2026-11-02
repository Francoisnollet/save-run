"""Bill diagnosis: where does the money go? Spend per task from an export of API calls.

Input CSV, one row per call, columns: date (YYYY-MM-DD), tache, modele, tokens_entree, tokens_sortie.
Prices come from prices.yaml (same file as `saverun run`); the currency is the file's, nothing is converted.
A model absent from the price file is counted apart and never valued at zero.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .cost import PriceTable, call_cost

DEFAULT_COVERAGE = 0.80          # share of the spend the selected tasks must cover
# Arbitrary default, in the price file's currency per month: below it, optimizing probably costs more than it
# saves. It is not derived from anything; set it according to the value of your own time.
DEFAULT_INTEREST_THRESHOLD = 100.0
MIN_DAYS = 7


class DiagnoseError(ValueError):
    """Unusable calls export. User-facing (French)."""


@dataclass
class TaskRow:
    task: str
    calls: int
    calls_per_month: float
    cost_per_month: float
    share: float
    cumulative: float
    main_model: str
    to_optimize: bool
    unpriced_calls: int


@dataclass
class Diagnosis:
    currency: str
    rows: list[TaskRow]
    n_calls: int
    first: date
    last: date
    days: int
    ignored_rows: int
    unpriced: dict[str, int]
    monthly_total: float
    selected: list[str]
    coverage: float
    interest_threshold: float
    warnings: list[str] = field(default_factory=list)

    @property
    def worth_optimizing(self) -> bool:
        return self.monthly_total >= self.interest_threshold


def diagnose(calls_path: str | Path, prices: PriceTable, coverage: float = DEFAULT_COVERAGE,
             interest_threshold: float = DEFAULT_INTEREST_THRESHOLD) -> Diagnosis:
    p = Path(calls_path)
    if not p.is_file():
        raise DiagnoseError(f"Export d'appels introuvable : {p}")
    tasks: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "unpriced": 0,
                                                   "models": defaultdict(int)})
    days, ignored, unpriced = [], 0, defaultdict(int)
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        needed = {"date", "tache", "modele", "tokens_entree", "tokens_sortie"}
        if not reader.fieldnames or not needed <= set(reader.fieldnames):
            raise DiagnoseError("L'export doit avoir les colonnes : " + ", ".join(sorted(needed)) + ".")
        for r in reader:
            try:
                d = date.fromisoformat(r["date"].strip()[:10])
                tin, tout = int(r["tokens_entree"]), int(r["tokens_sortie"])
                task, model = r["tache"].strip(), r["modele"].strip()
                if not task or not model or tin < 0 or tout < 0:
                    raise ValueError
            except (ValueError, AttributeError, TypeError):
                ignored += 1
                continue
            days.append(d)
            t = tasks[task]
            t["calls"] += 1
            t["models"][model] += 1
            price = prices.get(model)
            if price is None:
                t["unpriced"] += 1
                unpriced[model] += 1
            else:
                t["cost"] += call_cost(tin, tout, price)
    if not days:
        raise DiagnoseError("Aucun appel exploitable dans l'export.")

    span = (max(days) - min(days)).days + 1
    factor = 30 / span
    total = sum(t["cost"] for t in tasks.values())
    cumulative, selected, rows = 0.0, [], []
    for name, t in sorted(tasks.items(), key=lambda kv: (-kv[1]["cost"], kv[0])):
        share = t["cost"] / total if total else 0.0
        before, cumulative = cumulative, cumulative + share
        keep = bool(total) and before < coverage     # tasks are kept until the target share is covered
        if keep:
            selected.append(name)
        rows.append(TaskRow(name, t["calls"], t["calls"] * factor, t["cost"] * factor, share, cumulative,
                            max(t["models"], key=t["models"].get), keep, t["unpriced"]))

    used_models = sorted({m for t in tasks.values() for m in t["models"]})
    warnings = list(prices.warnings([m for m in used_models if prices.get(m)]))
    if span < MIN_DAYS:
        warnings.append(f"Moins de {MIN_DAYS} jours de données : la projection mensuelle est fragile.")
    if ignored:
        warnings.append(f"{ignored} ligne(s) illisible(s) ignorée(s).")
    if unpriced:
        missing = ", ".join(f"{m} ({n} appels)" for m, n in sorted(unpriced.items()))
        warnings.append(f"Modèle(s) absent(s) de {prices.path}, non valorisé(s) : {missing}. "
                        "Le total est sous-estimé.")
    if not total:
        warnings.append("Aucun appel n'a pu être valorisé : aucune tâche n'est retenue.")
    return Diagnosis(prices.currency, rows, len(days), min(days), max(days), span, ignored, dict(unpriced),
                     total * factor, selected, coverage, interest_threshold, warnings)


def write_csv(path: Path, d: Diagnosis) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["task", "calls_per_month", "cost_per_month", "currency", "share", "cumulative",
                    "main_model", "to_optimize", "unpriced_calls"])
        for r in d.rows:
            w.writerow([r.task, round(r.calls_per_month), round(r.cost_per_month, 2), d.currency,
                        round(r.share, 4), round(r.cumulative, 4), r.main_model, r.to_optimize, r.unpriced_calls])
