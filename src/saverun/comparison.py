"""Case-by-case comparison with the current prompt. Pure logic: no API call here.

The reference is the same as for the saving (see selection.compute_economy): BASE on current_model, i.e. what runs in
production today. Without base_prompt or current_model there is no reference and no assumption is made.

A case is "passed" when its success rate over its runs reaches config.success_rate. This only serves the comparison
and the critical cases: the score of a variant and its Wilson interval stay the mean of the case rates.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .scoring import case_rates
from .variants import BASE_ID

EPS = 1e-9


@dataclass(frozen=True)
class Comparison:
    """One (model, variant) against the reference, on the same cases. Every field holds case ids, sorted."""
    regressions: tuple[str, ...] = ()           # passed by the reference, failed by the variant
    corrections: tuple[str, ...] = ()           # failed by the reference, passed by the variant
    common_failures: tuple[str, ...] = ()       # failed by both
    unverified: tuple[str, ...] = ()            # no completed run on one side (failed calls): cannot be compared
    critical_regressions: tuple[str, ...] = ()  # the regressions on critical cases
    critical_unverified: tuple[str, ...] = ()   # the unverified critical cases


@dataclass(frozen=True)
class ComparisonContext:
    """What the comparison rests on, for the report."""
    reference: tuple[str, str] | None           # (current_model, "BASE"), when it could be measured
    reason: str | None                          # why there is no comparison; no assumption is ever made
    at_threshold: list[tuple[str, str, str]] = field(default_factory=list)  # (model, variant, case) at success_rate


def passed(rate: float, success_rate: float) -> bool:
    return rate + EPS >= success_rate


def reference_pair(config) -> tuple[tuple[str, str] | None, str | None]:
    """The reference couple, or the reason there is none."""
    if not config.base_prompt:
        return None, "aucun base_prompt dans la configuration : il n'y a pas de prompt actuel à comparer."
    if not config.current_model:
        return None, ("current_model n'est pas renseigné : on ne sait pas sur quel modèle tourne le prompt actuel, "
                      "aucune comparaison cas par cas n'est faite.")
    return (config.current_model, BASE_ID), None


def rates_by_couple(results, expected: dict[str, str], config, phase: str) -> dict[tuple[str, str], dict[str, float]]:
    """(model, variant) -> case -> success rate over its completed runs, for one phase."""
    groups: dict[tuple[str, str], list] = {}
    for r in results:
        if r.phase == phase and r.case in expected:
            groups.setdefault((r.model, r.variant), []).append(r)
    return {couple: case_rates(group, expected, config.scoring, config.fields) for couple, group in groups.items()}


def compare(reference: dict[str, float], rates: dict[str, float], cases, success_rate: float) -> Comparison:
    """Classify each case of `cases` (objects with .id and .critical) for one variant against the reference."""
    sets: dict[str, list[str]] = {k: [] for k in ("regressions", "corrections", "common_failures", "unverified")}
    for c in cases:
        if c.id not in reference or c.id not in rates:
            sets["unverified"].append(c.id)
            continue
        ref_ok, ok = passed(reference[c.id], success_rate), passed(rates[c.id], success_rate)
        if ref_ok and not ok:
            sets["regressions"].append(c.id)
        elif ok and not ref_ok:
            sets["corrections"].append(c.id)
        elif not ok:
            sets["common_failures"].append(c.id)
    critical = {c.id for c in cases if c.critical}
    out = {k: tuple(sorted(v)) for k, v in sets.items()}
    return Comparison(**out, critical_regressions=tuple(x for x in out["regressions"] if x in critical),
                      critical_unverified=tuple(x for x in out["unverified"] if x in critical))


def critical_passed(rates: dict[str, float], cases, success_rate: float) -> tuple[int, int]:
    """(critical cases passed, critical cases with a completed run). The Wilson interval is computed on these."""
    done = [c.id for c in cases if c.critical and c.id in rates]
    return sum(passed(rates[i], success_rate) for i in done), len(done)


def at_threshold(rates: dict[tuple[str, str], dict[str, float]], success_rate: float) -> list[tuple[str, str, str]]:
    """Cases whose rate is exactly success_rate (e.g. 3 runs out of 5 at 0.6): passed, but only just."""
    return sorted((m, v, case) for (m, v), by_case in rates.items() for case, rate in by_case.items()
                  if abs(rate - success_rate) < EPS)


def check_critical_validation(validations: list, results, cases, config, reference: tuple[str, str] | None) -> list:
    """Validations (selection.Validation) checked against the reference on the critical validation cases.

    A candidate that breaks more than max_regressions_critiques of them is no longer confirmed. A case without a
    completed run on one side (failed calls, or the reference not run) is unverified, never a regression. The
    candidate is never replaced: this only turns a confirmation off. The reference itself is not checked.
    """
    critical = [c for c in cases if c.critical]
    if not critical:
        return validations
    validations = [replace(v, critical_n=len(critical)) for v in validations]
    if reference is None:
        return validations
    rates = rates_by_couple(results, {c.id: c.expected for c in critical}, config, "validation")
    out = []
    for v in validations:
        if (v.model, v.variant) == reference:
            out.append(v)
            continue
        c = compare(rates.get(reference, {}), rates.get((v.model, v.variant), {}), critical, config.success_rate)
        breaks = len(c.critical_regressions) > config.max_regressions_critiques
        out.append(replace(v, confirmed=v.confirmed and not breaks, critical_checked=True,
                           critical_regressions=c.critical_regressions, critical_unverified=c.critical_unverified))
    return out


def compare_rows(rows: list, results, cases, config, phase: str = "selection") -> tuple[list, ComparisonContext]:
    """Rows (selection.Row) with their comparison and critical counts attached. Scores and statuses are not touched.

    A row already marked (model not tested, no completed call) gets no comparison. Without a measurable reference,
    no row gets one and the context says why.
    """
    expected = {c.id: c.expected for c in cases}
    rates = rates_by_couple(results, expected, config, phase)
    counted = []
    for r in rows:
        if not r.status:
            k, n = critical_passed(rates.get((r.model, r.variant), {}), cases, config.success_rate)
            r = replace(r, critical_passed=k, critical_n=n)
        counted.append(r)
    rows = counted
    near = at_threshold(rates, config.success_rate) if config.runs_per_case > 1 else []
    ref, reason = reference_pair(config)
    if ref is None:
        return rows, ComparisonContext(None, reason, near)
    ref_row = next((r for r in rows if (r.model, r.variant) == ref), None)
    if ref_row is None or ref_row.status or not rates.get(ref):
        why = f" (statut : {ref_row.status})" if ref_row is not None and ref_row.status else ""
        return rows, ComparisonContext(None, f"le prompt actuel n'a pas pu être mesuré sur {ref[0]}{why} : aucune "
                                             "comparaison cas par cas n'est faite.", near)
    out = [r if r.status else replace(r, comparison=compare(rates[ref], rates.get((r.model, r.variant), {}), cases,
                                                            config.success_rate))
           for r in rows]
    return out, ComparisonContext(ref, None, near)
