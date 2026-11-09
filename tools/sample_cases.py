#!/usr/bin/env python3
"""Tire un échantillon équilibré d'un fichier étiqueté (texte, étiquette) et l'écrit en cases.csv pour SAVE Run.

    python tools/sample_cases.py private/jeu/test.csv --labels-file private/jeu/labels.csv --per-label 17 --seed 0 \\
        -o private/jeu/cases.csv

Entrée : un CSV avec une ligne d'en-tête ; les colonnes du texte et de l'étiquette sont choisies par leur nom
(--text-column, --label-column ; défaut : text et category).
Étiquettes retenues : --labels a,b,c, ou --labels-file, un CSV à une ou deux colonnes (étiquette, tag facultatif,
par exemple une famille). Une ligne d'en-tête « label » est ignorée.
Tirage : --per-label cas par étiquette, sans remise. Il est reproductible : pour une graine donnée, le tirage d'une
étiquette ne dépend ni de l'ordre des lignes du fichier, ni des autres étiquettes demandées.
Sortie : colonnes `input,expected` (expected = l'étiquette, pour scoring: exact), plus `tag` si un tag est donné.

Utilitaire autonome (bibliothèque standard seulement), hors du paquet saverun. Il ne contacte aucun service.
Gardez les données dans data/ ou private/ : ces dossiers sont ignorés par git.
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from pathlib import Path


class SampleError(ValueError):
    """Unusable input. User-facing (French)."""


def read_labels(spec: str | None, path: Path | None) -> dict[str, str]:
    """Label -> tag ("" when none), in the given order."""
    if spec is not None:
        labels = [x.strip() for x in spec.split(",") if x.strip()]
        out = {label: "" for label in labels}
    else:
        if not path.is_file():
            raise SampleError(f"Fichier d'étiquettes introuvable : {path}")
        out = {}
        with path.open(newline="", encoding="utf-8-sig") as f:
            for number, row in enumerate(csv.reader(f), start=1):
                row = [cell.strip() for cell in row]
                if not any(row):
                    continue
                if number == 1 and row[0].casefold() == "label":
                    continue
                if len(row) > 2:
                    raise SampleError(f"{path.name}, ligne {number} : {len(row)} colonnes au lieu de 1 ou 2.")
                label, tag = row[0], row[1] if len(row) == 2 else ""
                if label in out:
                    raise SampleError(f"{path.name}, ligne {number} : étiquette « {label} » en double.")
                out[label] = tag
    if not out:
        raise SampleError("Aucune étiquette demandée.")
    return out


def read_pool(path: Path, text_column: str, label_column: str,
              delimiter: str = ",") -> tuple[dict[str, list[str]], list[str]]:
    """Label -> distinct texts. Exact duplicates are kept once; a text found under two labels is dropped."""
    if not path.is_file():
        raise SampleError(f"Fichier introuvable : {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        missing = [c for c in (text_column, label_column) if c not in (reader.fieldnames or [])]
        if missing:
            raise SampleError(f"{path.name} : colonne(s) absente(s) : {', '.join(missing)} "
                              f"(colonnes trouvées : {', '.join(reader.fieldnames or [])}).")
        labels_of: dict[str, set[str]] = {}
        rows = 0
        for row in reader:
            text, label = (row[text_column] or "").strip(), (row[label_column] or "").strip()
            if text and label:
                rows += 1
                labels_of.setdefault(text, set()).add(label)
    warnings = []
    duplicates = rows - sum(len(v) for v in labels_of.values())
    if duplicates:
        warnings.append(f"{duplicates} ligne(s) en double (même texte, même étiquette) gardée(s) une seule fois.")
    ambiguous = [t for t, ls in labels_of.items() if len(ls) > 1]
    if ambiguous:
        warnings.append(f"{len(ambiguous)} texte(s) présent(s) sous plusieurs étiquettes : écarté(s) du tirage.")
    pool: dict[str, list[str]] = {}
    for text, ls in labels_of.items():
        if len(ls) == 1:
            pool.setdefault(next(iter(ls)), []).append(text)
    return pool, warnings


def sample(pool: dict[str, list[str]], labels: dict[str, str], per_label: int,
           seed: int) -> list[tuple[str, str, str]]:
    """(text, label, tag) rows, per_label for each label, shuffled together. Fails rather than return fewer."""
    unknown = [label for label in labels if label not in pool]
    if unknown:
        raise SampleError(f"Étiquette(s) absente(s) du fichier : {', '.join(unknown)}.")
    short = [f"{label} ({len(pool[label])})" for label in labels if len(pool[label]) < per_label]
    if short:
        raise SampleError(f"Pas assez de cas pour {per_label} par étiquette : {', '.join(short)}.")
    rows = []
    for label in sorted(labels):
        texts = sorted(pool[label])                          # independent of the file's row order
        picked = random.Random(f"{seed}:{label}").sample(texts, per_label)
        rows += [(text, label, labels[label]) for text in picked]
    random.Random(seed).shuffle(rows)                        # the file is not grouped by label
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Échantillon équilibré (texte, étiquette) -> cases.csv pour SAVE Run.")
    ap.add_argument("input", type=Path, help="CSV avec en-tête : une colonne de texte, une colonne d'étiquette")
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--labels", help="étiquettes retenues, séparées par des virgules")
    which.add_argument("--labels-file", type=Path, help="CSV : étiquette, tag facultatif (une ligne par étiquette)")
    ap.add_argument("--per-label", type=int, required=True, help="nombre de cas par étiquette")
    ap.add_argument("--seed", type=int, default=0, help="graine du tirage (défaut : 0)")
    ap.add_argument("--text-column", default="text", help="défaut : text")
    ap.add_argument("--label-column", default="category", help="défaut : category")
    ap.add_argument("--delimiter", default=",", choices=[",", ";", "\t"], help="séparateur (défaut : virgule)")
    ap.add_argument("-o", "--output", type=Path, default=Path("cases.csv"), help="défaut : cases.csv")
    ap.add_argument("--force", action="store_true", help="écraser le fichier de sortie s'il existe")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.per_label < 1:
        print("Erreur : --per-label doit être au moins 1.", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        print(f"Erreur : {args.output} existe déjà (utilisez --force pour l'écraser).", file=sys.stderr)
        return 2
    try:
        labels = read_labels(args.labels, args.labels_file)
        pool, warnings = read_pool(args.input, args.text_column, args.label_column, args.delimiter)
        rows = sample(pool, labels, args.per_label, args.seed)
    except SampleError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2
    tagged = any(labels.values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["input", "expected", *(["tag"] if tagged else [])])
        w.writerows((text, label, *([tag] if tagged else [])) for text, label, tag in rows)
    print(f"{len(rows)} cas écrits dans {args.output} : {args.per_label} par étiquette, "
          f"{len(labels)} étiquettes, graine {args.seed}.")
    for w_ in warnings:
        print(f"! {w_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
