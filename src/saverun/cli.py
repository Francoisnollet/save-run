"""Command-line entry point. The engine lives in the other modules; this file only wires arguments and prompts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from . import __version__
from .config import ConfigError, load_config, load_dotenv
from .cost import PriceError, load_prices
from .diagnose import (DEFAULT_COVERAGE, DEFAULT_INTEREST_THRESHOLD, DiagnoseError, diagnose, write_csv)
from .pipeline import Plan, execute_plan, prepare
from .report import SIMULATION_BANNER, num, pct, print_summary, write_outputs
from .runner import MaxCallsError, MissingKeyError, RunStore, build_runtimes
from .scoring import ScoringError
from .splitting import CasesError, load_cases

USER_ERRORS = (ConfigError, CasesError, PriceError, ScoringError, MissingKeyError, MaxCallsError, DiagnoseError)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="saverun", description="Le couple prompt x modèle le moins cher qui "
                                     "démontre un seuil de qualité.")
    parser.add_argument("--version", action="version", version=f"saverun {__version__}")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="générer des variantes, les tester, recommander et chiffrer l'économie")
    run.add_argument("--config", default="config.yaml")
    run.add_argument("--cases", default="cases.csv")
    run.add_argument("--prices", default="prices.yaml")
    run.add_argument("--env", default=".env", help="fichier .env local contenant les clés d'API")
    run.add_argument("--out", help="dossier de sortie (défaut : saverun-out, ou saverun-out/dry-run en simulation)")
    run.add_argument("--dry-run", action="store_true", help="faux modèle : tout le pipeline, aucun appel, aucun coût")
    run.add_argument("--seed", type=int, help="--dry-run seulement : change le hasard du faux modèle")
    run.add_argument("--yes", action="store_true", help="ne pas demander confirmation avant les appels payants")
    run.add_argument("--no-footer", action="store_true", help="retirer la ligne finale de report.md")
    diag = sub.add_parser("diagnose", help="où part l'argent : dépense par tâche à partir d'un export d'appels")
    diag.add_argument("calls", help="CSV : date, tache, modele, tokens_entree, tokens_sortie")
    diag.add_argument("--prices", default="prices.yaml")
    diag.add_argument("--part", type=float, default=DEFAULT_COVERAGE,
                      help="part de la dépense que les tâches retenues doivent couvrir (défaut : 0,80)")
    diag.add_argument("--seuil-interet", type=float, default=DEFAULT_INTEREST_THRESHOLD,
                      help="dépense mensuelle, dans la monnaie de prices.yaml, sous laquelle l'optimisation ne vaut "
                           f"sans doute pas le temps passé. Valeur par défaut ARBITRAIRE ({DEFAULT_INTEREST_THRESHOLD:g}) : "
                           "à fixer selon votre offre.")
    diag.add_argument("--out", default="saverun-out/diagnostic.csv")
    return parser


def cmd_diagnose(args: argparse.Namespace, console: Console) -> int:
    d = diagnose(args.calls, load_prices(args.prices), args.part, args.seuil_interet)
    cur = d.currency
    console.print(f"{d.n_calls} appels sur {d.days} jour(s), du {d.first} au {d.last}.", highlight=False)
    from rich.table import Table
    t = Table()
    for name, justify in [("Tâche", "left"), ("Appels/mois", "right"), (f"{cur}/mois", "right"),
                          ("Part", "right"), ("Cumul", "right"), ("Modèle principal", "left"), ("", "left")]:
        t.add_column(name, justify=justify, no_wrap=True)
    for r in d.rows:
        t.add_row(r.task, f"{r.calls_per_month:.0f}", num(r.cost_per_month, 2), pct(r.share), pct(r.cumulative),
                  r.main_model, "◄" if r.to_optimize else "")
    console.print(t)
    console.print(f"Total valorisé : {num(d.monthly_total, 2)} {cur} par mois au rythme observé "
                  f"({num(d.monthly_total * 12, 0)} {cur} par an).", highlight=False)
    if not d.worth_optimizing:
        console.print(f"-> Sous {args.seuil_interet:g} {cur} par mois : l'optimisation ne se justifie sans doute pas "
                      "(seuil arbitraire, réglable avec --seuil-interet).", highlight=False)
    elif d.selected:
        console.print(f"-> À optimiser en priorité ({pct(d.coverage)} de la dépense) : {', '.join(d.selected)}",
                      highlight=False)
    console.print("◄ = tâche retenue pour un test SAVE Run.", highlight=False)
    for w in d.warnings:
        console.print(f"! {w}", highlight=False)
    write_csv(Path(args.out), d)
    console.print(f"Détail écrit dans {args.out}", highlight=False)
    return 0


def _print_plan(plan: Plan, console: Console) -> None:
    cfg, pf, cur = plan.config, plan.preflight, plan.prices.currency
    if plan.dry_run:
        console.print(f"[bold white on red] {SIMULATION_BANNER} [/]")
    per_model = ", ".join(f"{m} : {n}" for m, n in pf.calls_by_model.items()) or "aucun"
    console.print(f"{len(plan.all_variants)} variante(s){' dont la référence BASE' if plan.base else ''} × "
                  f"{len(cfg.models)} modèle(s) × {len(plan.split.selection)} cas de sélection × "
                  f"{cfg.runs_per_case} passage(s) = {pf.total_jobs} appels.", highlight=False)
    console.print(f"  {pf.reused} déjà en cache, {pf.new_calls} à faire ({per_model}).", highlight=False)
    console.print(f"Validation, après la sélection : {plan.validation_calls_per_candidate} appels par candidat "
                  "(le gagnant et ses quasi-égaux).", highlight=False)
    console.print(f"Coût maximum estimé : {num(pf.max_cost)} {cur} pour la sélection, plus au plus "
                  f"{num(plan.validation_cost_ceiling)} {cur} par candidat validé.", highlight=False)
    console.print(plan.reachability[0], highlight=False)
    for w in plan.warnings:
        console.print(f"! {w}", highlight=False)


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    load_dotenv(args.env)
    config = load_config(args.config)
    cases, case_warnings = load_cases(args.cases)
    prices = load_prices(args.prices)
    out = Path(args.out) if args.out else Path("saverun-out") / ("dry-run" if args.dry_run else "")
    # Simulated answers must never be mistaken for real ones: separate store, even with an explicit --out.
    store = RunStore(out / "runs" / "simulation" if args.dry_run else out / "runs")

    plan = prepare(config, cases, prices, store, dry_run=args.dry_run, case_warnings=case_warnings)
    if args.seed is not None and not args.dry_run:
        console.print("! --seed ne concerne que --dry-run : ignoré.")
    runtimes = build_runtimes(config, dry_run=args.dry_run, expectations={c.input: c.expected for c in cases},
                              seed=args.seed)  # checks every API key before anything starts
    _print_plan(plan, console)

    if not args.dry_run and not args.yes and plan.preflight.new_calls:
        if input("Lancer ? Ces appels sont payants. [o/N] ").strip().lower() != "o":
            console.print("Annulé.")
            return 1

    done = [0]

    def progress(_result) -> None:
        done[0] += 1
        if done[0] % 200 == 0:
            console.print(f"  … {done[0]} appels traités", highlight=False)

    try:
        outcome = execute_plan(plan, runtimes, store, progress=progress)
    except KeyboardInterrupt:
        console.print("Interrompu. Les appels déjà faits sont dans runs/ : relancez pour reprendre sans les repayer.")
        return 130
    paths, notes = write_outputs(outcome, out, footer=not args.no_footer)
    console.print()
    print_summary(outcome, console)
    for n in [*outcome.notes, *notes]:
        console.print(f"! {n}", highlight=False)
    console.print("\nFichiers écrits : " + ", ".join(str(p) for p in paths), highlight=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    console = Console()
    try:
        return cmd_diagnose(args, console) if args.command == "diagnose" else cmd_run(args, console)
    except USER_ERRORS as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
