#!/usr/bin/env python3
"""Note de chaque variante × modèle par famille d'e-mails, à partir des appels déjà faits par un run.

    python tools/per_family.py saverun-out/discriminant private/emails_discriminants_familles.csv

Entrées : le dossier de sortie d'un run (avec runs/calls.jsonl) et un CSV à colonnes texte, urgent (oui/non), famille.
Sortie : <dossier>/par_famille.md, régénéré à chaque lancement : une ligne par variante × modèle, une colonne par
famille, sur les cas de sélection uniquement, puis les e-mails que le plus de couples ratent.

Chaque appel est relié à sa famille par le hash du texte de l'e-mail. La notation est celle de SAVE Run (réponse en
échec exclue, intervalle de Wilson à 95 %), et les totaux sont comparés à results.csv quand il existe.

Utilitaire hors du paquet saverun, qu'il importe sans le modifier. Il ne contacte aucun service et ne lit pas .env.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from saverun.report import pct
from saverun.runner import CallResult, RunStore, _sha
from saverun.scoring import case_rates, wilson

OUTPUT_NAME = "par_famille.md"
EXCERPT_LENGTH = 100
LABELS = {"oui": True, "non": False}


class InputError(ValueError):
    """Unusable input. User-facing (French)."""


@dataclass(frozen=True)
class Email:
    text: str
    family: str
    urgent: bool


@dataclass(frozen=True)
class Analysis:
    families: list[str]
    rates: dict[tuple[str, str], dict[str, float]]   # (model, variant) -> email hash -> success rate
    emails: dict[str, Email]                         # email hash -> email
    warnings: list[str]


def load_emails(path: Path) -> dict[str, Email]:
    """Read the families file, keyed by the hash SAVE Run stores for each call's input."""
    if not path.is_file():
        raise InputError(f"fichier introuvable : {path}")
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"), newline=""))
    if not reader.fieldnames or not {"texte", "urgent", "famille"} <= set(reader.fieldnames):
        raise InputError(f"{path.name} doit avoir les colonnes texte, urgent et famille.")
    emails: dict[str, Email] = {}
    for number, row in enumerate(reader, start=2):
        text, family = (row["texte"] or "").strip(), (row["famille"] or "").strip()
        label = LABELS.get((row["urgent"] or "").strip().lower())
        if not text or not family or label is None:
            raise InputError(f"ligne {number} de {path.name} : texte ou famille vide, ou urgent différent de oui/non.")
        emails[_sha(text)] = Email(text, family, label)
    return emails


def analyse(calls: list[CallResult], emails: dict[str, Email], field: str = "urgent") -> Analysis:
    """Per-email success rates of each couple, on the selection phase only."""
    selection = [c for c in calls if c.phase == "selection"]
    warnings = []
    unlinked = {c.input_hash for c in selection} - set(emails)
    if unlinked:
        warnings.append(f"{len(unlinked)} e-mail(s) de sélection absents du fichier de familles : ils sont ignorés. "
                        "Le fichier est-il bien celui du run ?")

    # The store is append-only: after a config change, an older prompt can sit under the same variant name.
    latest = {(c.model, c.variant): c.prompt_hash for c in selection}
    stale = {(c.model, c.variant) for c in selection if c.prompt_hash != latest[(c.model, c.variant)]}
    if stale:
        warnings.append(f"{len(stale)} couple(s) ont plusieurs prompts dans runs/ : seul le plus récent est compté.")

    expected = {h: json.dumps({field: e.urgent}) for h, e in emails.items()}
    by_couple: dict[tuple[str, str], list[CallResult]] = {}
    for c in selection:
        if c.input_hash in emails and c.prompt_hash == latest[(c.model, c.variant)]:
            by_couple.setdefault((c.model, c.variant), []).append(replace(c, case=c.input_hash))
    rates = {couple: case_rates(group, expected, "json_fields", [field]) for couple, group in by_couple.items()}
    tested = {h for per_email in rates.values() for h in per_email}
    return Analysis(sorted({emails[h].family for h in tested}), rates, emails, warnings)


def family_score(per_email: dict[str, float], emails: dict[str, Email], family: str) -> tuple[float, int]:
    values = [rate for h, rate in per_email.items() if emails[h].family == family]
    return sum(values), len(values)


def compare_with_results(path: Path, a: Analysis) -> tuple[int, list[str]]:
    """Check each couple's total against results.csv. Returns (couples that match, warnings)."""
    if not path.is_file():
        return 0, ["results.csv absent : les totaux n'ont pas pu être comparés à ceux du run."]
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = {(r["model"], r["variant"]): r for r in csv.DictReader(f)}
    matching, warnings = 0, []
    for couple, per_email in sorted(a.rates.items()):
        row = rows.get(couple)
        n = len(per_email)
        if row is None:
            warnings.append(f"{couple[1]} sur {couple[0]} : absent de results.csv.")
        elif int(row["n_cases"]) != n or abs(float(row["score"]) - sum(per_email.values()) / n) > 1e-3:
            warnings.append(f"{couple[1]} sur {couple[0]} : {n} cas ici contre {row['n_cases']} dans results.csv, "
                            f"ou note différente.")
        else:
            matching += 1
    return matching, warnings


def hardest(a: Analysis) -> list[tuple[int, int, Email]]:
    """Emails missed by at least one couple, most missed first: (couples that missed, couples tested, email)."""
    out = []
    for h, email in a.emails.items():
        seen = [per_email[h] for per_email in a.rates.values() if h in per_email]
        missed = sum(rate < 1 for rate in seen)
        if missed:
            out.append((missed, len(seen), email))
    return sorted(out, key=lambda item: (-item[0], item[2].family, item[2].text))


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return _cell(flat[:EXCERPT_LENGTH] + ("…" if len(flat) > EXCERPT_LENGTH else ""))


def _score_cell(k: float, n: int) -> str:
    if not n:
        return "—"
    lo, hi = wilson(k, n)
    return f"{k:g}/{n} : {pct(k / n)} ({pct(lo)} – {pct(hi)})"


def render(a: Analysis, source: str, top: int, matching: int) -> str:
    lines = ["# Notes par famille", "",
             f"Cas de sélection uniquement, familles lues dans {source}. Chaque cellule : réussites/total : note "
             "(intervalle de Wilson à 95 %). Les réponses en échec sont exclues.", "",
             "Une famille compte peu de cas : ces intervalles sont larges et ne servent pas à choisir un couple.", "",
             "| Variante | Modèle | " + " | ".join(_cell(f) for f in a.families) + " |",
             "|---|---|" + "---|" * len(a.families)]
    for (model, variant), per_email in sorted(a.rates.items()):
        cells = [_score_cell(*family_score(per_email, a.emails, f)) for f in a.families]
        lines.append(f"| {variant} | {model} | " + " | ".join(cells) + " |")
    lines += ["", "## E-mails les plus souvent ratés", ""]
    missed = hardest(a)
    if missed:
        lines += ["| Couples qui ratent | Famille | Urgent attendu | Début du texte |", "|---|---|---|---|"]
        lines += [f"| {m}/{n} | {_cell(e.family)} | {'oui' if e.urgent else 'non'} | {_excerpt(e.text)} |"
                  for m, n, e in missed[:top]]
        if len(missed) > top:
            lines += ["", f"… et {len(missed) - top} autre(s) e-mail(s) raté(s) au moins une fois."]
    else:
        lines.append("Aucun : tous les couples ont réussi tous les e-mails de sélection.")
    lines += ["", "## Contrôles", "",
              f"- Totaux identiques à results.csv pour {matching} couple(s) sur {len(a.rates)}."]
    lines += [f"- ! {w}" for w in a.warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Note de chaque variante × modèle par famille d'e-mails.")
    ap.add_argument("run_dir", type=Path, help="dossier de sortie du run, avec runs/calls.jsonl")
    ap.add_argument("families", type=Path, help="CSV à colonnes texte, urgent (oui/non), famille")
    ap.add_argument("--field", default="urgent", help="champ JSON noté (défaut : urgent)")
    ap.add_argument("--top", type=int, default=10, help="nombre d'e-mails ratés à lister (défaut : 10)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    store = RunStore(args.run_dir / "runs")
    try:
        if not store.path.is_file():
            raise InputError(f"{store.path} introuvable (une simulation écrit dans runs/simulation/ et ne compte pas).")
        a = analyse(store.read_all(), load_emails(args.families), args.field)
        if not a.rates:
            raise InputError("aucun appel de sélection abouti ne peut être relié au fichier de familles.")
    except InputError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2
    matching, warnings = compare_with_results(args.run_dir / "results.csv", a)
    a = replace(a, warnings=[*a.warnings, *warnings])

    out = args.run_dir / OUTPUT_NAME
    with out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(render(a, args.families.name, args.top, matching))
    print(f"{len(a.rates)} couples, {len(a.families)} familles : {out}")
    print(f"Totaux identiques à results.csv pour {matching} couple(s) sur {len(a.rates)}.")
    for w in a.warnings:
        print(f"! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
