"""Orchestration of a run: plan (nothing is called), then execute. The engine, importable without the CLI.

    plan = prepare(config, cases, prices, store, dry_run=False)   # splits, variants, jobs, pre-launch figures
    outcome = execute_plan(plan, runtimes, store)                 # selection, then validation
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .cost import PriceTable, call_cost, estimate_tokens
from .runner import (CallResult, Job, MaxCallsError, Preflight, RunStore, build_jobs, execute, preflight,
                     untested_models)
from .scoring import closed_labels, expected_label, validate_expected
from .selection import (Economy, Row, Selection, Validation, build_rows, compute_economy, reachability, select,
                        validate, validation_pairs)
from .splitting import Case, Split, split_cases, tag_warnings
from .variants import Variant, base_variant, build_variants


@dataclass
class Plan:
    config: Config
    split: Split
    variants: list[Variant]           # generated variants V01...
    base: Variant | None              # the current prompt, when given
    prices: PriceTable
    jobs: list[Job]                   # selection phase
    preflight: Preflight
    validation_calls_per_candidate: int
    validation_cost_ceiling: float    # worst case for one validated candidate
    reachability: tuple[str, bool]
    warnings: list[str]
    dry_run: bool

    @property
    def all_variants(self) -> list[Variant]:
        return self.variants + ([self.base] if self.base else [])


@dataclass(frozen=True)
class ValidationQuote:
    """What validating one candidate would still cost: calls missing from the store, priced at its own model."""
    model: str
    variant: str
    is_winner: bool
    new_calls: int
    max_cost: float                   # upper estimate, in the price file's currency
    estimated: bool                   # free model priced at its reference tariff


VALIDATION_DECLINED = ("Validation refusée avant les appels payants : {n} appel(s) non faits, les candidats "
                       "concernés ne sont pas validés. Relancez pour la faire : la sélection est reprise du cache.")


@dataclass
class RunOutcome:
    plan: Plan
    selection_results: list[CallResult]
    validation_results: list[CallResult]
    rows: list[Row]
    selection: Selection
    validations: list[Validation]
    economy: Economy
    untested: dict[str, str]
    notes: list[str] = field(default_factory=list)

    @property
    def variant_by_id(self) -> dict[str, Variant]:
        return {v.id: v for v in self.plan.all_variants}


def prepare(config: Config, cases: list[Case], prices: PriceTable, store: RunStore | None, *,
            dry_run: bool, case_warnings: list[str] | None = None) -> Plan:
    """Everything that can be decided and checked before the first call. Raises on any blocking problem."""
    validate_expected([c.expected for c in cases], config.scoring, config.fields)
    split = split_cases(cases, config.validation_part, config.seed,
                        label=lambda c: expected_label(c.expected, config.scoring, config.fields))
    variants, variant_warnings = build_variants(config, split.examples)
    base = base_variant(config)
    all_variants = variants + ([base] if base else [])
    jobs = build_jobs(all_variants, config, split.selection, "selection")
    pf = preflight(jobs, prices, None, store)

    val_calls = len(split.validation) * config.runs_per_case
    if config.max_calls is not None and pf.new_calls + val_calls > config.max_calls:
        raise MaxCallsError(f"{pf.new_calls} appels de sélection + {val_calls} appels de validation "
                            f"(pour un seul candidat) dépassent max_calls ({config.max_calls}). "
                            "Réduisez n_variants, le nombre de modèles ou de cas, ou relevez max_calls.")
    ceiling = max(
        sum(call_cost(estimate_tokens(v.system + c.input), config.max_tokens, prices.models[m.id])
            for c in split.validation) * config.runs_per_case
        for m in config.models for v in all_variants)

    return Plan(config=config, split=split, variants=variants, base=base, prices=prices, jobs=jobs, preflight=pf,
                validation_calls_per_candidate=val_calls, validation_cost_ceiling=ceiling,
                reachability=reachability(len(split.selection), config.seuil_qualite),
                warnings=[*(case_warnings or []), *split.warnings, *tag_warnings(split.selection), *config.warnings,
                          *variant_warnings,
                          *pf.warnings],
                dry_run=dry_run)


def validation_quotes(plan: Plan, pairs: list[tuple[str, str]], store: RunStore,
                      winner: tuple[str, str] | None = None) -> list[ValidationQuote]:
    """For each candidate, the validation calls not yet in the store and their maximum cost at its model's price."""
    cfg, done = plan.config, store.load_ok()
    quotes = []
    for model, variant in pairs:
        price = plan.prices.models[model]
        pending = [j for j in build_jobs(plan.all_variants, cfg, plan.split.validation, "validation",
                                         [(model, variant)]) if j.key not in done]
        cost = sum(call_cost(estimate_tokens(j.system + j.input), cfg.max_tokens, price) for j in pending)
        quotes.append(ValidationQuote(model, variant, (model, variant) == winner, len(pending), cost, price.free))
    return quotes


def execute_plan(plan: Plan, runtimes, store: RunStore, *, progress: Callable[[CallResult], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 confirm_validation: Callable[[list[ValidationQuote]], bool] | None = None) -> RunOutcome:
    """Run the selection phase, select, then validate the winner and its near-ties. Validation never re-selects.

    `confirm_validation`, when given, is asked before any validation call that is not already in the store. If it
    declines, only fully cached candidates are validated (none, unless the winner is one of them).
    """
    cfg, split = plan.config, plan.split
    sel_results = execute(plan.jobs, runtimes, store, progress=progress, sleep=sleep)
    every_case = split.examples + split.selection + split.validation
    labels = closed_labels([c.expected for c in every_case], cfg.scoring)
    raw_rows = build_rows(sel_results, {c.id: c.expected for c in split.selection}, plan.all_variants, cfg, plan.prices,
                          labels=labels)
    selection = select(raw_rows, cfg.seuil_qualite)
    notes: list[str] = []

    pairs = _affordable_pairs(plan, selection, store, notes)
    declined = False
    if pairs and confirm_validation is not None:
        winner = (selection.winner.model, selection.winner.variant)
        quotes = validation_quotes(plan, pairs, store, winner)
        if any(q.new_calls for q in quotes) and not confirm_validation(quotes):
            declined = True
            pairs = [(q.model, q.variant) for q in quotes if q.new_calls == 0]
            notes.append(VALIDATION_DECLINED.format(n=sum(q.new_calls for q in quotes)))
    val_results: list[CallResult] = []
    if pairs:
        jobs = build_jobs(plan.all_variants, cfg, split.validation, "validation", pairs)
        val_results = execute(jobs, runtimes, store, progress=progress, sleep=sleep)
    validations = validate(val_results, {c.id: c.expected for c in split.validation}, selection, cfg)
    if declined:  # report only what was actually validated; without the winner, nothing is
        kept = [v for v in validations if (v.model, v.variant) in pairs]
        validations = kept if any(v.is_winner for v in kept) else []
    if selection.winner and not validations and not declined:
        notes.append("La validation n'a pas pu être exécutée : le gagnant reste « non confirmé ».")

    return RunOutcome(plan, sel_results, val_results, selection.rows, selection, validations,
                      compute_economy(selection, cfg), untested_models(sel_results + val_results), notes)


def _affordable_pairs(plan: Plan, selection: Selection, store: RunStore, notes: list[str]) -> list[tuple[str, str]]:
    """The candidates to validate, trimmed from the last near-tie so that max_calls is respected."""
    pairs = validation_pairs(selection)
    cap = plan.config.max_calls
    if cap is None or not pairs:
        return pairs
    budget = cap - plan.preflight.new_calls
    done = store.load_ok()
    kept: list[tuple[str, str]] = []
    for pair in pairs:
        jobs = build_jobs(plan.all_variants, plan.config, plan.split.validation, "validation", [pair])
        pending = sum(j.key not in done for j in jobs)
        if pending > budget:
            break
        budget -= pending
        kept.append(pair)
    if len(kept) < len(pairs):
        notes.append(f"max_calls : {len(pairs) - len(kept)} quasi-égal(aux) non validé(s) "
                     "(le gagnant passe toujours en premier).")
    return kept
