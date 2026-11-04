"""Outputs: terminal table, results.csv, report.md, best_prompt.txt and best_config.json.

Wording is French. Nothing here calls an API, and nothing here ever contains an API key (only the *name* of
the environment variable appears in best_config.json).
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from .pipeline import RunOutcome
from .runner import MAX_FAILED_SHARE
from .selection import NON_TESTE, PLUS_CHERE, RETENUE, Row, not_separated, winner_confirmed

SIMULATION_BANNER = "SIMULATION : faux modèle, résultats sans valeur"
FOOTER = "Généré avec SAVE Run (https://allrun.be)"

CSV_FIELDS = ["model", "variant", "levers", "score", "ci_low", "ci_high", "n_cases", "avg_tokens_in",
              "avg_tokens_out", "cost_per_1000", "currency", "estimated_cost", "status", "failed_share",
              "truncated_share", "simulation"]


# --- Formatting --------------------------------------------------------------------

SHORT_STATUS = {PLUS_CHERE: "démontré, + chère"}   # terminal only
TRUNCATED_ERRORS_FROM = 0.02      # from this share of truncated answers, the report says what they explain


def pct(x: float, digits: int = 0) -> str:
    return f"{x * 100:.{digits}f}".replace(".", ",") + " %"


def num(x: float | None, digits: int = 3) -> str:
    return "—" if x is None else f"{x:.{digits}f}".replace(".", ",")


def _temperature(model) -> str:
    return "défaut du fournisseur" if model.temperature is None else f"{model.temperature:g}"


# --- Plain-text building blocks (shared by the terminal and report.md) ---------------------

def verdict(o: RunOutcome) -> str:
    cfg, sel = o.plan.config, o.selection
    cur = o.plan.prices.currency
    w = sel.winner
    if w is None:
        return (f"Aucune variante ne démontre {pct(cfg.seuil_qualite)} de qualité (borne basse de l'intervalle "
                "à 95 %) : baissez le seuil ou ajoutez des cas de test.")
    text = (f"{w.variant} sur {w.model} est la moins chère à démontrer {pct(cfg.seuil_qualite)} de qualité "
            f"(note {pct(w.score)}, borne basse {pct(w.lo)}), pour {num(w.cost_per_1000)} {cur} les 1 000 appels"
            f"{' (coût estimé)' if w.estimated else ''}")
    confirmed = winner_confirmed(o.validations)
    if confirmed is True:
        text += ", confirmée sur les cas de validation."
    elif confirmed is False:
        text += ", mais NON CONFIRMÉE sur les cas de validation : ajoutez des cas avant de l'appliquer."
    else:
        text += ". La validation n'a pas pu être exécutée."
    if w.unfair:
        text += (f" Non comparable équitablement ({pct(w.failed_share)} d'appels en échec) : à relancer.")
    return text


def separation_lines(o: RunOutcome) -> list[str]:
    return not_separated(o.rows, o.plan.all_variants, o.plan.config.seuil_qualite)


def economy_lines(o: RunOutcome) -> list[str]:
    eco, cfg, cur = o.economy, o.plan.config, o.plan.prices.currency
    ref, lines = eco.reference, []
    if ref is not None:
        lines.append(f"Prompt actuel sur {ref.model} : coût {num(ref.cost_per_1000)} {cur} les 1 000 appels, "
                     f"note {pct(ref.score)} (borne basse {pct(ref.lo)}, {ref.n_cases} cas).")
    if eco.reason:
        lines.append(f"Aucun chiffre d'économie : {eco.reason}")
        return lines
    s, w = eco.savings, o.selection.winner
    if eco.winner_is_reference:
        lines.append("Le prompt actuel sur ce modèle est déjà le couple retenu : aucune économie à réaliser.")
    elif s.per_1000 >= 0:
        line = (f"Économie : {num(s.per_1000)} {cur} par 1 000 appels ({pct(s.percent)}) par rapport au prompt "
                f"actuel sur {ref.model}")
        if s.monthly is not None:
            line += f", soit {num(s.monthly, 2)} {cur} par mois pour {cfg.monthly_volume:g} appels mensuels"
        lines.append(line + ".")
    else:
        lines.append(f"Le couple retenu coûte plus cher que le prompt actuel ({pct(-s.percent)} de plus, "
                     f"{num(-s.per_1000)} {cur} par 1 000 appels) : il n'y a pas d'économie.")
    if eco.equal_quality:
        lines.append("À qualité égale : le prompt actuel démontre lui aussi le seuil.")
    else:
        lines.append(f"Attention : l'économie n'est pas « à qualité égale ». Le prompt actuel ne démontre pas "
                     f"lui-même {pct(cfg.seuil_qualite)} (note {pct(ref.score)}, borne basse {pct(ref.lo)}).")
    if w.estimated or ref.estimated:
        lines.append("Un des modèles est gratuit : son coût est estimé au tarif de référence, ce n'est pas "
                     "une dépense réelle.")
    return lines


def validation_lines(o: RunOutcome) -> list[str]:
    cfg, vals = o.plan.config, o.validations
    if not vals:
        if o.selection.winner is None:
            return ["Aucune validation : aucune variante n'est retenue."]
        return ["La validation n'a pas pu être exécutée : le gagnant reste non confirmé."]
    lines = []
    for v in vals:
        tag = "gagnant" if v.is_winner else "quasi-égal"
        lines.append(f"{v.variant} sur {v.model} ({tag}) : {pct(v.score)} sur {v.n_cases} cas "
                     f"(intervalle {pct(v.lo)} – {pct(v.hi)}) → {'confirmé' if v.confirmed else 'non confirmé'}")
    w = next(v for v in vals if v.is_winner)
    if w.indicative:
        lines.append(f"Indicatif : l'ensemble de validation ne compte que {w.n_planned} cas (moins de 20).")
    if not w.confirmed:
        lines.append(f"Non confirmée : la note sur la validation est sous {pct(cfg.seuil_qualite)}. Aucun autre "
                     "candidat n'est substitué (ce serait re-sélectionner sur la validation) ; ajoutez des cas "
                     "(visez 50) et relancez.")
    if len(vals) > 1:
        lines.append("Les quasi-égaux sont validés à titre d'information : la recommandation ne change pas.")
    return lines


def price_notes(o: RunOutcome) -> list[str]:
    prices = o.plan.prices
    used = [m.id for m in o.plan.config.models]
    lines = [f"Monnaie du fichier de prix : {prices.currency} (aucune conversion)."]
    lines += [f"{m} : relevé du {prices.models[m].date}, source : {prices.models[m].source}"
              f"{' (tarif de référence, modèle gratuit)' if prices.models[m].free else ''}" for m in used]
    return lines


def truncation_lines(o: RunOutcome) -> list[str]:
    cut = [r for r in o.rows if r.truncated_share > 0]
    if not cut:
        return ["Aucune réponse tronquée."]
    items = ", ".join(f"{r.variant} sur {r.model} ({pct(r.truncated_share)})" for r in cut)
    return [f"Réponses tronquées (max_tokens = {o.plan.config.max_tokens}) : {items}.",
            "Augmentez max_tokens (surtout pour de l'extraction JSON) : une réponse tronquée est notée comme les "
            "autres, donc le plus souvent fausse."]


def truncated_error_lines(o: RunOutcome) -> list[str]:
    """How many wrong answers were truncated, for variants with enough truncated answers. Scores stay as they are."""
    lines = []
    for r in o.rows:
        if r.truncated_share + 1e-9 < TRUNCATED_ERRORS_FROM or not r.wrong_truncated:
            continue
        n, m, name = r.wrong_truncated, r.wrong_answers, f"{r.variant} sur {r.model}"
        lines.append(f"Les erreurs de {name} viennent surtout des troncatures ({n} sur {m})." if 2 * n > m
                     else f"{n} erreur(s) sur {m} de {name} viennent de troncatures.")
    return lines


def failure_lines(o: RunOutcome) -> list[str]:
    unfair = [r for r in o.rows if r.unfair]
    lines = []
    if unfair:
        items = ", ".join(f"{r.variant} sur {r.model} ({pct(r.failed_share)})" for r in unfair)
        lines.append(f"Plus de {pct(MAX_FAILED_SHARE)} d'appels en échec, donc non comparables équitablement : "
                     f"{items}. À relancer.")
    for model, reason in o.untested.items():
        lines.append(f"Modèle non testé : {model} — {reason}.")
    return lines or ["Aucun modèle non testé ; aucune variante avec trop d'appels en échec."]


# --- results.csv ---------------------------------------------------------------------

def write_results_csv(path: Path, o: RunOutcome) -> None:
    cur = o.plan.prices.currency
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in o.rows:
            w.writerow({
                "model": r.model, "variant": r.variant, "levers": r.description, "score": round(r.score, 4),
                "ci_low": round(r.lo, 4), "ci_high": round(r.hi, 4), "n_cases": r.n_cases,
                "avg_tokens_in": "" if r.avg_in is None else round(r.avg_in, 1),
                "avg_tokens_out": "" if r.avg_out is None else round(r.avg_out, 1),
                "cost_per_1000": "" if r.cost_per_1000 is None else round(r.cost_per_1000, 6), "currency": cur,
                "estimated_cost": r.estimated, "status": r.status, "failed_share": round(r.failed_share, 4),
                "truncated_share": round(r.truncated_share, 4), "simulation": o.plan.dry_run})


# --- best_prompt.txt and best_config.json ----------------------------------------------

def best_config(o: RunOutcome) -> dict:
    w = o.selection.winner
    variant = o.variant_by_id[w.variant]
    model = next(m for m in o.plan.config.models if m.id == w.model)
    val = next((v for v in o.validations if v.is_winner), None)
    shots = variant.shots or 0
    return {
        "simulation": False,
        "model": {"id": model.id, "provider": model.provider, "base_url": model.base_url,
                  "api_key_env": model.api_key_env, "temperature": model.temperature},  # null: provider default
        "max_tokens": o.plan.config.max_tokens,
        "prompt_file": "best_prompt.txt",
        "prompt": variant.system,
        "variant": {"id": variant.id, "description": variant.description, "levers": variant.levers},
        "examples_included": [{"input": e.input, "expected": e.expected} for e in o.plan.split.examples[:shots]],
        "scoring": o.plan.config.scoring, "fields": o.plan.config.fields,
        "quality_threshold": o.plan.config.seuil_qualite,
        "selection": {"score": round(w.score, 4), "ci_low": round(w.lo, 4), "ci_high": round(w.hi, 4),
                      "n_cases": w.n_cases, "cost_per_1000": round(w.cost_per_1000, 6),
                      "currency": o.plan.prices.currency, "estimated_cost": w.estimated},
        "validation": None if val is None else {"confirmed": val.confirmed, "score": round(val.score, 4),
                                                "ci_low": round(val.lo, 4), "ci_high": round(val.hi, 4),
                                                "n_cases": val.n_cases, "indicative": val.indicative},
        "to_rerun": w.unfair,  # too many failed calls: not fairly comparable
        "prices": {"date": o.plan.prices.models[model.id].date, "source": o.plan.prices.models[model.id].source},
    }


# --- report.md -----------------------------------------------------------------------

def _md_table(o: RunOutcome) -> list[str]:
    cur = o.plan.prices.currency
    head = (f"| Variante | Modèle | Leviers | Note | Intervalle 95 % | Cas | Tokens entrée | Tokens sortie | "
            f"Coût / 1 000 appels ({cur}) | Échecs | Tronquées | Statut |")
    out = [head, "|" + "---|" * 12]
    for r in o.rows:
        status = r.status + (" — non comparable équitablement" if r.unfair else "")
        out.append(f"| {r.variant} | {r.model} | {r.description} | {pct(r.score)} | {pct(r.lo)} – {pct(r.hi)} | "
                   f"{r.n_cases}/{r.n_planned} | {num(r.avg_in, 0)} | {num(r.avg_out, 0)} | "
                   f"{num(r.cost_per_1000)}{' *' if r.estimated else ''} | {pct(r.failed_share)} | "
                   f"{pct(r.truncated_share)} | {status} |")
    if any(r.estimated for r in o.rows):
        out += ["", "\\* coût estimé au tarif de référence (modèle gratuit)."]
    return out


def render_report(o: RunOutcome, footer: bool = True) -> str:
    cfg, split = o.plan.config, o.plan.split
    L: list[str] = ["# Rapport SAVE Run", ""]
    if o.plan.dry_run:
        L += [f"> **{SIMULATION_BANNER}**", ""]
    L += ["## Verdict", "", verdict(o), ""]
    for x in separation_lines(o):
        L += [f"**Attention.** {x}", ""]
    L += ["## Économie", ""]
    L += [f"- {x}" for x in economy_lines(o)]
    L += ["", "## Validation", ""]
    L += [f"- {x}" for x in validation_lines(o)]
    L += ["", "## Résultats", "", f"Seuil de qualité : {pct(cfg.seuil_qualite)} sur la borne basse de l'intervalle "
          "de Wilson à 95 %. Lignes triées par coût.", ""]
    L += _md_table(o)
    L += ["", "## Conditions et réserves", ""]
    L += [f"- Cas : {len(split.examples) + len(split.selection) + len(split.validation)} au total "
          f"({len(split.examples)} exemples few-shot, {len(split.selection)} de sélection, "
          f"{len(split.validation)} de validation), graine {cfg.seed}.",
          f"- {o.plan.reachability[0]}",
          f"- Passages par cas : {cfg.runs_per_case}."]
    L += [f"- Modèle {m.id} : température : {_temperature(m)}." for m in cfg.models]
    L += [f"- {x}" for x in truncation_lines(o)]
    L += [f"- {x}" for x in truncated_error_lines(o)]
    L += [f"- {x}" for x in failure_lines(o)]
    L += ["", "## Prix", ""] + [f"- {x}" for x in price_notes(o)]
    caveats = [*o.plan.prices.warnings([m.id for m in cfg.models]), *o.plan.warnings, *o.notes]
    caveats = list(dict.fromkeys(caveats))
    if caveats:
        L += ["", "## Avertissements", ""] + [f"- {x}" for x in caveats]
    if footer:
        L += ["", "---", FOOTER]
    return "\n".join(L) + "\n"


# --- Writing -------------------------------------------------------------------------

def write_outputs(o: RunOutcome, out_dir: Path, footer: bool = True) -> tuple[list[Path], list[str]]:
    """Write the files; returns (paths written, notes). In a simulation, best_* are never written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written, notes = [], []
    write_results_csv(out_dir / "results.csv", o)
    written.append(out_dir / "results.csv")
    (out_dir / "report.md").write_text(render_report(o, footer), encoding="utf-8")
    written.append(out_dir / "report.md")
    if o.plan.dry_run:
        notes.append("Simulation : best_prompt.txt et best_config.json ne sont pas écrits.")
    elif o.selection.winner is None:
        notes.append("Aucune variante retenue : best_prompt.txt et best_config.json ne sont pas écrits.")
    else:
        (out_dir / "best_prompt.txt").write_text(o.variant_by_id[o.selection.winner.variant].system + "\n",
                                                 encoding="utf-8")
        (out_dir / "best_config.json").write_text(json.dumps(best_config(o), ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")
        written += [out_dir / "best_prompt.txt", out_dir / "best_config.json"]
    return written, notes


# --- Terminal ------------------------------------------------------------------------

def render_table(o: RunOutcome) -> Table:
    cur = o.plan.prices.currency
    title = f"SAVE Run — seuil {pct(o.plan.config.seuil_qualite)} sur la borne basse"
    if o.plan.dry_run:
        title = f"{SIMULATION_BANNER.upper()}\n{title}"
    t = Table(title=title, title_style="bold red" if o.plan.dry_run else "bold", box=box.SIMPLE_HEAD,
              show_edge=False, pad_edge=False, collapse_padding=True)
    names = short_model_names([r.model for r in o.rows])
    show_fail = any(r.failed_share for r in o.rows)
    show_cut = any(r.truncated_share for r in o.rows)
    cols = [("Var.", "left"), ("Modèle", "left"), ("Note", "right"), ("IC 95 %", "right"), (f"{cur}/1000", "right")]
    cols += ([("Échecs", "right")] if show_fail else []) + ([("Tronq.", "right")] if show_cut else [])
    for name, justify in cols:
        t.add_column(name, justify=justify, no_wrap=True)
    t.add_column("Statut")  # the one column allowed to wrap, so that a narrow window never crushes the figures
    for r in o.rows:
        status = SHORT_STATUS.get(r.status, r.status) + (" (non comp.)" if r.unfair else "")
        cells = [r.variant + (" *" if r.status == RETENUE else ""), names[r.model], pct(r.score),
                 f"{pct(r.lo)}-{pct(r.hi)}", num(r.cost_per_1000) + ("~" if r.estimated else "")]
        cells += ([pct(r.failed_share)] if show_fail else []) + ([pct(r.truncated_share)] if show_cut else [])
        t.add_row(*cells, status,
                  style="bold" if r.status == RETENUE else ("dim" if r.status == NON_TESTE else None))
    return t


def short_model_names(models: list[str]) -> dict[str, str]:
    """Model id -> short name for the terminal. Two ids that would read the same both keep their full id."""
    ids = list(dict.fromkeys(models))
    short = {m: re.sub(r"-\d{8}$", "", m.rsplit("/", 1)[-1].removeprefix("claude-")) for m in ids}
    seen = Counter(short.values())
    return {m: s if seen[s] == 1 else m for m, s in short.items()}


def print_summary(o: RunOutcome, console: Console) -> None:
    if o.plan.dry_run:
        console.print(f"[bold white on red] {SIMULATION_BANNER} [/]")
    console.print(render_table(o))
    console.print("* = couple retenu (le moins cher dont la borne basse atteint le seuil) ; ~ = coût estimé.",
                  highlight=False)
    legend = [f"{s} = {m}" for m, s in short_model_names([r.model for r in o.rows]).items() if s != m]
    if legend:
        console.print("Modèles : " + " ; ".join(legend), highlight=False)
    console.print("Détail complet (leviers, tokens, cas, échecs) dans report.md.", highlight=False)
    console.print(f"\n[bold]Verdict[/] : {verdict(o)}", highlight=False)
    for line in separation_lines(o):
        console.print(f"  ! {line}", highlight=False)
    for line in economy_lines(o):
        console.print(f"  {line}", highlight=False)
    console.print("\n[bold]Validation[/]", highlight=False)
    for line in validation_lines(o):
        console.print(f"  {line}", highlight=False)
    flags = []
    if o.untested or any(r.unfair for r in o.rows):
        flags += failure_lines(o)
    if any(r.truncated_share for r in o.rows):
        flags += truncation_lines(o)
    for line in flags:
        console.print(f"  ! {line}", highlight=False)
    if o.plan.dry_run:
        console.print(f"[bold white on red] {SIMULATION_BANNER} [/]")
