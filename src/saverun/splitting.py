"""Loading cases and splitting them into few-shot examples, selection set and validation set.

The split is done once, before any execution, with a fixed seed. A case is never in two sets.
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from typing import Callable
from pathlib import Path

N_EXAMPLES = 3
MIN_CASES = 20          # below this, results are only indicative
MIN_PER_SET = 2
MIN_CASES_PER_TAG = 10  # fewer selection cases than this under a tag: its score says very little


UNTAGGED = "(sans tag)"


class CasesError(ValueError):
    """Unusable cases file. User-facing (French)."""


@dataclass(frozen=True)
class Case:
    id: str        # stable reference (row number in the file), used in runs/
    input: str
    expected: str
    tag: str = ""  # optional group, for the "Note par tag" section only: never sent, never used to split or to select


@dataclass(frozen=True)
class Split:
    examples: list[Case]
    selection: list[Case]
    validation: list[Case]
    warnings: list[str] = field(default_factory=list)


def load_cases(path: str | Path) -> tuple[list[Case], list[str]]:
    """Read cases.csv (columns input, expected, optional tag). Exact duplicate inputs are dropped with a warning."""
    p = Path(path)
    if not p.is_file():
        raise CasesError(f"Fichier de cas introuvable : {p}")
    with p.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"input", "expected"} <= set(reader.fieldnames):
            raise CasesError("cases.csv doit avoir les colonnes « input » et « expected ».")
        rows = list(reader)
    cases, seen, seen_tags, warnings, dropped = [], {}, {}, [], 0
    for i, r in enumerate(rows, start=2):  # line 1 is the header
        inp, exp, tag = (r["input"] or "").strip(), (r["expected"] or "").strip(), (r.get("tag") or "").strip()
        if not inp and not exp:
            continue
        if not inp or not exp:
            raise CasesError(f"Ligne {i} de {p.name} : « input » ou « expected » vide.")
        if inp in seen:
            dropped += 1
            if seen[inp] != exp:
                warnings.append(f"Ligne {i} : même input que la ligne précédente avec un autre « expected » "
                                "(ignorée) ; vérifiez vos données.")
            elif seen_tags[inp] != tag:
                warnings.append(f"Ligne {i} : même input que la ligne précédente avec un autre tag (ignorée) ; "
                                "vérifiez vos données.")
            continue
        seen[inp], seen_tags[inp] = exp, tag
        cases.append(Case(id=f"C{i - 1:03d}", input=inp, expected=exp, tag=tag))
    if dropped:
        warnings.append(f"{dropped} cas en double (même input) ignoré(s) : un cas ne doit jamais "
                        "se retrouver dans deux ensembles.")
    untagged = sum(not c.tag for c in cases)
    if 0 < untagged < len(cases):
        warnings.append(f"{untagged} cas sans tag : ils sont regroupés sous « {UNTAGGED} » dans la note par tag.")
    return cases, warnings


def tag_warnings(selection: list[Case]) -> list[str]:
    """Tags with too few selection cases. The draw does not balance tags: this is where it shows."""
    counts: dict[str, int] = {}
    for c in selection:
        if c.tag:
            counts[c.tag] = counts.get(c.tag, 0) + 1
    few = [f"{tag} ({n})" for tag, n in sorted(counts.items()) if n < MIN_CASES_PER_TAG]
    if not few:
        return []
    return [f"Moins de {MIN_CASES_PER_TAG} cas de sélection pour ce(s) tag(s) : {', '.join(few)}. Leur note par tag "
            "n'est qu'indicative."]


def _pick_examples(shuffled: list[Case], n: int, label: Callable[[Case], str],
                   rnd: random.Random) -> tuple[list[Case], list[str]]:
    """Few-shot examples with at least one case per distinct expected value, when n allows it.

    Values are taken in order of first appearance in the shuffled cases, the remaining slots are filled with the
    next shuffled cases, and the order of the examples is shuffled so it does not follow the values.
    Returns (examples, warnings).
    """
    first: dict[str, Case] = {}
    for c in shuffled:
        first.setdefault(label(c), c)
    picked = [first[v] for v in list(first)[:n]]
    taken = {c.id for c in picked}
    picked += [c for c in shuffled if c.id not in taken][:n - len(picked)]
    rnd.shuffle(picked)
    warnings = []
    if len(first) > n:
        missing = [v for v in list(first)[n:]]
        shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        warnings.append(f"{len(first)} valeurs attendues distinctes pour {n} exemples few-shot seulement : "
                        f"{len(missing)} valeur(s) ne figurent pas dans les exemples ({shown}).")
    return picked, warnings


def split_cases(cases: list[Case], validation_part: float, seed: int,
                n_examples: int = N_EXAMPLES, label: Callable[[Case], str] | None = None) -> Split:
    """Partition the cases: few-shot examples first, then validation, the rest is the selection set.

    `label` gives the expected value a case asks for (default: its expected text, normalized); the examples
    cover every distinct value when there are enough slots, and a warning says so when there are not.
    """
    needed = n_examples + 2 * MIN_PER_SET
    if len(cases) < needed:
        raise CasesError(f"{len(cases)} cas : il en faut au moins {needed} pour séparer exemples, "
                         "sélection et validation (20 au minimum, 50 recommandés).")
    label = label or (lambda c: c.expected.strip().casefold())
    shuffled = list(cases)
    rnd = random.Random(seed)
    rnd.shuffle(shuffled)
    examples, example_warnings = _pick_examples(shuffled, n_examples, label, rnd)
    chosen = {c.id for c in examples}
    rest = [c for c in shuffled if c.id not in chosen]
    n_val = min(max(MIN_PER_SET, round(len(rest) * validation_part)), len(rest) - MIN_PER_SET)
    validation, selection = rest[:n_val], rest[n_val:]

    warnings = list(example_warnings)
    if len(cases) < MIN_CASES:
        warnings.append(f"{len(cases)} cas seulement : le résultat est indicatif (visez 20 à 50 cas).")
    return Split(examples, selection, validation, warnings)
