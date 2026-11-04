#!/usr/bin/env python3
"""Convertit un fichier à deux ou trois colonnes (texte de l'e-mail, urgent oui/non, tag) en cases.csv pour SAVE Run.

    python tools/make_cases.py data/emails.csv -o cases.csv

Entrée : une ligne par e-mail : le texte, puis oui ou non, puis un tag facultatif (par exemple une famille de cas),
qui peut rester vide. Une ligne d'en-tête est détectée toute seule. Le séparateur (virgule, point-virgule ou tabulation) est détecté ; un texte qui contient le séparateur ou
des retours à la ligne doit être entre guillemets, comme tout export CSV de tableur.
Sortie : colonnes `input,expected`, avec expected = {"urgent": true} ou {"urgent": false} correctement échappé,
plus une colonne `tag` si au moins une ligne en a un.

Utilitaire autonome (bibliothèque standard seulement), hors du paquet saverun. Il ne contacte aucun service.
Gardez vos fichiers d'e-mails réels dans data/ ou private/ : ils sont ignorés par git.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path

YES = {"oui", "o", "yes", "y", "true", "vrai", "1"}
NO = {"non", "n", "no", "false", "faux", "0"}
DELIMITERS = ",;\t"
HINT = " (le texte contient-il le séparateur sans guillemets ?)"


def parse_label(cell: str) -> bool | None:
    token = cell.strip().lower()
    return True if token in YES else False if token in NO else None


def detect_delimiter(sample: str) -> str:
    first = sample.splitlines()[0] if sample.strip() else ""
    counts = {d: first.count(d) for d in DELIMITERS}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def convert(path: Path, field: str = "urgent", delimiter: str | None = None):
    """Return (cases, errors, warnings): cases is a list of (text, expected_json, label, tag)."""
    raw = path.read_text(encoding="utf-8-sig")
    delim = delimiter or detect_delimiter(raw)
    cases, errors, warnings, seen = [], [], [], Counter()
    reader = csv.reader(io.StringIO(raw, newline=""), delimiter=delim)
    for number, row in enumerate(reader, start=1):
        if not any(cell.strip() for cell in row):
            continue
        if number == 1 and len(row) >= 2 and parse_label(row[1]) is None and parse_label(row[-1]) is None:
            continue  # header line
        if len(row) not in (2, 3):
            hint = HINT if len(row) > 3 else ""
            errors.append(f"ligne {number} : {len(row)} colonne(s) au lieu de 2 ou 3{hint}.")
            continue
        text, label, tag = row[0].strip(), parse_label(row[1]), row[2].strip() if len(row) == 3 else ""
        if not text:
            errors.append(f"ligne {number} : texte vide.")
        elif label is None:
            hint = HINT if len(row) == 3 else ""
            errors.append(f"ligne {number} : « {row[1].strip()[:20]} » n'est ni oui ni non{hint}.")
        else:
            seen[text] += 1
            cases.append((text, json.dumps({field: label}, ensure_ascii=False), label, tag))
    duplicates = sum(n - 1 for n in seen.values())
    if duplicates:
        warnings.append(f"{duplicates} e-mail(s) en double : SAVE Run ne gardera que le premier de chaque.")
    return cases, errors, warnings


def balance_warnings(cases: list[tuple[str, str, bool, str]]) -> list[str]:
    out = []
    n = len(cases)
    positives = sum(case[2] for case in cases)
    if n < 20:
        out.append(f"{n} cas seulement : SAVE Run avertira que le résultat est indicatif (visez 50).")
    if n and min(positives, n - positives) < max(5, round(0.1 * n)):
        out.append(f"classes très déséquilibrées ({positives} oui, {n - positives} non) : une réponse "
                   f"constante aurait déjà {max(positives, n - positives) / n:.0%} de réussite.")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fichier à deux ou trois colonnes (texte, oui/non, tag) -> cases.csv pour SAVE Run.")
    ap.add_argument("input", type=Path, help="colonnes : texte de l'e-mail, urgent oui/non, tag (facultatif)")
    ap.add_argument("-o", "--output", type=Path, default=Path("cases.csv"), help="défaut : cases.csv")
    ap.add_argument("--field", default="urgent", help="nom du champ JSON (défaut : urgent)")
    ap.add_argument("--delimiter", choices=list(DELIMITERS), help="séparateur (détecté par défaut)")
    ap.add_argument("--force", action="store_true", help="écraser le fichier de sortie s'il existe")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not args.input.is_file():
        print(f"Erreur : fichier introuvable : {args.input}", file=sys.stderr)
        return 2
    if args.output.exists() and not args.force:
        print(f"Erreur : {args.output} existe déjà (utilisez --force pour l'écraser).", file=sys.stderr)
        return 2
    cases, errors, warnings = convert(args.input, args.field, args.delimiter)
    if errors:
        print("Erreur : fichier non converti.", file=sys.stderr)
        for e in errors[:10]:
            print(f"  - {e}", file=sys.stderr)
        if len(errors) > 10:
            print(f"  … et {len(errors) - 10} autre(s).", file=sys.stderr)
        return 2
    if not cases:
        print("Erreur : aucune ligne exploitable.", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        tagged = any(case[3] for case in cases)
        w.writerow(["input", "expected", *(["tag"] if tagged else [])])
        w.writerows((text, expected, *([tag] if tagged else [])) for text, expected, _, tag in cases)
    yes = sum(case[2] for case in cases)
    print(f"{len(cases)} cas écrits dans {args.output} ({yes} oui, {len(cases) - yes} non).")
    for w_ in [*warnings, *balance_warnings(cases)]:
        print(f"! {w_}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
