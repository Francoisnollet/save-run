"""Loading cases and splitting them into few-shot examples, selection set and validation set.

The split is done once, before any execution, with a fixed seed. A case is never in two sets.
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from pathlib import Path

N_EXAMPLES = 3
MIN_CASES = 20          # below this, results are only indicative
MIN_PER_SET = 2


class CasesError(ValueError):
    """Unusable cases file. User-facing (French)."""


@dataclass(frozen=True)
class Case:
    id: str        # stable reference (row number in the file), used in runs/
    input: str
    expected: str


@dataclass(frozen=True)
class Split:
    examples: list[Case]
    selection: list[Case]
    validation: list[Case]
    warnings: list[str] = field(default_factory=list)


def load_cases(path: str | Path) -> tuple[list[Case], list[str]]:
    """Read cases.csv (columns input, expected). Exact duplicate inputs are dropped with a warning."""
    p = Path(path)
    if not p.is_file():
        raise CasesError(f"Fichier de cas introuvable : {p}")
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"input", "expected"} <= set(reader.fieldnames):
            raise CasesError("cases.csv doit avoir les colonnes « input » et « expected ».")
        rows = list(reader)
    cases, seen, warnings, dropped = [], {}, [], 0
    for i, r in enumerate(rows, start=2):  # line 1 is the header
        inp, exp = (r["input"] or "").strip(), (r["expected"] or "").strip()
        if not inp and not exp:
            continue
        if not inp or not exp:
            raise CasesError(f"Ligne {i} de {p.name} : « input » ou « expected » vide.")
        if inp in seen:
            dropped += 1
            if seen[inp] != exp:
                warnings.append(f"Ligne {i} : même input que la ligne précédente avec un autre « expected » "
                                "(ignorée) ; vérifiez vos données.")
            continue
        seen[inp] = exp
        cases.append(Case(id=f"C{i - 1:03d}", input=inp, expected=exp))
    if dropped:
        warnings.append(f"{dropped} cas en double (même input) ignoré(s) : un cas ne doit jamais "
                        "se retrouver dans deux ensembles.")
    return cases, warnings


def split_cases(cases: list[Case], validation_part: float, seed: int,
                n_examples: int = N_EXAMPLES) -> Split:
    """Partition the cases: few-shot examples first, then validation, the rest is the selection set."""
    needed = n_examples + 2 * MIN_PER_SET
    if len(cases) < needed:
        raise CasesError(f"{len(cases)} cas : il en faut au moins {needed} pour séparer exemples, "
                         "sélection et validation (20 au minimum, 50 recommandés).")
    shuffled = list(cases)
    random.Random(seed).shuffle(shuffled)
    examples, rest = shuffled[:n_examples], shuffled[n_examples:]
    n_val = min(max(MIN_PER_SET, round(len(rest) * validation_part)), len(rest) - MIN_PER_SET)
    validation, selection = rest[:n_val], rest[n_val:]

    warnings = []
    if len(cases) < MIN_CASES:
        warnings.append(f"{len(cases)} cas seulement : le résultat est indicatif (visez 20 à 50 cas).")
    return Split(examples, selection, validation, warnings)
