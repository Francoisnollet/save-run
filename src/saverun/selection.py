"""Selection by threshold on the Wilson lower bound, near-ties, and validation. Pure logic: no API call here.

Rules (see BRIEF.md): the unit is the case; the winner is the cheapest variant whose lower bound reaches the
threshold, never the best raw score; validation only confirms, it never re-selects.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .config import Config
from .cost import PriceTable, Savings, cost_per_1000, savings
from .runner import MAX_FAILED_SHARE, CallResult, untested_models
from .scoring import case_rates, max_lower_bound, score_output, variant_score
from .splitting import UNTAGGED, Case
from .variants import BASE_ID

NEAR_TIE_MARGIN = 0.10      # within 10 % of the winner's cost
INDICATIVE_BELOW = 20       # a validation set smaller than this only gives an indication
EPS = 1e-9

RETENUE = "retenue"
QUASI = "quasi à égalité"
PLUS_CHERE = "seuil démontré, plus chère"
SOUS_SEUIL = "sous le seuil"
NON_TESTE = "non testé"
SANS_REPONSE = "aucun appel abouti"


@dataclass(frozen=True)
class Row:
    model: str
    variant: str
    description: str
    n_cases: int                 # cases with at least one completed run: the statistical unit
    n_planned: int
    score: float
    lo: float
    hi: float
    avg_in: float | None
    avg_out: float | None
    cost_per_1000: float | None
    estimated: bool              # free model priced at its reference tariff
    failed_share: float
    truncated_share: float
    status: str = ""
    note: str = ""
    wrong_answers: int = 0            # completed answers scored wrong (per answer, not per case)
    wrong_truncated: int = 0          # those that were also truncated

    @property
    def unfair(self) -> bool:
        """Too many failed calls for a fair comparison with the other variants."""
        return self.failed_share > MAX_FAILED_SHARE


@dataclass(frozen=True)
class Selection:
    rows: list[Row]              # sorted by cost, statuses assigned
    winner: Row | None
    near_ties: list[Row]
    threshold: float


@dataclass(frozen=True)
class Validation:
    model: str
    variant: str
    n_cases: int
    n_planned: int
    score: float
    lo: float                    # always shown; not used to decide
    hi: float
    confirmed: bool              # raw score on the validation set reaches the threshold
    indicative: bool             # validation set smaller than INDICATIVE_BELOW
    is_winner: bool


# --- Reachability ------------------------------------------------------------------

def min_cases_for(threshold: float) -> int:
    """Smallest number of cases for which a perfect score can have a lower bound >= threshold."""
    n = 1
    while max_lower_bound(n) + EPS < threshold:
        n += 1
    return n


def _pct(x: float, digits: int = 0) -> str:
    return f"{x * 100:.{digits}f}".replace(".", ",") + " %"


def reachability(n_selection_cases: int, threshold: float) -> tuple[str, bool]:
    """(message, reachable): shown before launch, so the user sees whether the threshold can be demonstrated."""
    best = max_lower_bound(n_selection_cases)
    msg = f"Avec {n_selection_cases} cas de sélection, la borne basse maximale possible est {_pct(best, 1)}."
    if best + EPS >= threshold:
        return msg, True
    return (msg + f" Le seuil de {_pct(threshold)} est inatteignable : il faut au moins "
            f"{min_cases_for(threshold)} cas de sélection, ou un seuil plus bas."), False


# --- Rows --------------------------------------------------------------------------

def _scored(group: list[CallResult], expected: dict[str, str], config: Config) -> tuple[int, float, float, float]:
    rates = case_rates(group, expected, config.scoring, config.fields)
    score, lo, hi = variant_score(list(rates.values()))
    return len(rates), score, lo, hi


def build_rows(results: list[CallResult], expected: dict[str, str], variants, config: Config,
               prices: PriceTable, phase: str = "selection") -> list[Row]:
    """One row per (model, variant): score, interval, mean tokens reported by the API, cost per 1 000 calls.

    Failed calls are left out of the score and of the mean tokens. A model stopped after repeated key
    refusals is marked "non testé" and never competes.
    """
    prices.require(sorted({r.model for r in results}))
    descriptions = {v.id: v.description for v in variants}
    untested = untested_models(results)
    groups: dict[tuple[str, str], list[CallResult]] = {}
    for r in results:
        if r.phase == phase:
            groups.setdefault((r.model, r.variant), []).append(r)
    rows = []
    for (model, variant), group in groups.items():
        price = prices.models[model]
        attempted = [r for r in group if r.status != "skipped"]
        done = [r for r in group if r.ok]
        n_planned = len({r.case for r in group})
        base = dict(model=model, variant=variant, description=descriptions.get(variant, ""),
                    n_planned=n_planned, estimated=price.free)
        if model in untested:
            rows.append(Row(**base, n_cases=0, score=0.0, lo=0.0, hi=0.0, avg_in=None, avg_out=None,
                            cost_per_1000=None, failed_share=0.0, truncated_share=0.0,
                            status=NON_TESTE, note=untested[model]))
            continue
        n, score, lo, hi = _scored(group, expected, config)
        failed_share = (len(attempted) - len(done)) / len(attempted) if attempted else 0.0
        if not done:
            rows.append(Row(**base, n_cases=0, score=0.0, lo=0.0, hi=0.0, avg_in=None, avg_out=None,
                            cost_per_1000=None, failed_share=failed_share, truncated_share=0.0,
                            status=SANS_REPONSE))
            continue
        avg_in = sum(r.tokens_in for r in done) / len(done)
        avg_out = sum(r.tokens_out for r in done) / len(done)
        wrong = [r for r in done if not score_output(r.output, expected[r.case], config.scoring, config.fields).correct]
        rows.append(Row(**base, n_cases=n, score=score, lo=lo, hi=hi, avg_in=avg_in, avg_out=avg_out,
                        cost_per_1000=cost_per_1000(avg_in, avg_out, price), failed_share=failed_share,
                        truncated_share=sum(r.truncated for r in done) / len(done), wrong_answers=len(wrong),
                        wrong_truncated=sum(r.truncated for r in wrong)))
    return rows


# --- Selection ---------------------------------------------------------------------

def _order(r: Row) -> tuple:
    return (r.cost_per_1000 is None, r.cost_per_1000 or 0.0, r.model, r.variant)


def select(rows: list[Row], threshold: float) -> Selection:
    """Cheapest row whose lower bound reaches the threshold. Raw score never decides."""
    eligible = [r for r in rows if not r.status and r.n_cases > 0 and r.lo + EPS >= threshold]
    winner = min(eligible, key=lambda r: (r.cost_per_1000, -r.lo, r.model, r.variant), default=None)
    out = []
    for r in sorted(rows, key=_order):
        if r.status:                        # already "non testé" or "aucun appel abouti"
            out.append(r)
        elif winner is not None and r is winner:
            out.append(replace(r, status=RETENUE))
        elif r in eligible:
            near = r.cost_per_1000 <= winner.cost_per_1000 * (1 + NEAR_TIE_MARGIN) + EPS
            out.append(replace(r, status=QUASI if near else PLUS_CHERE))
        else:
            out.append(replace(r, status=SOUS_SEUIL))
    final_winner = next((r for r in out if r.status == RETENUE), None)
    ties = [r for r in out if r.status == QUASI]
    return Selection(out, final_winner, ties, threshold)


# --- Does the test set separate the variants? ---------------------------------------

NOT_SEPARATED_ALL = ("Presque toutes les variantes obtiennent la même note : le jeu de test ne permet pas de les "
                     "départager. Le choix repose surtout sur le coût. Ajoutez des cas plus difficiles ou comparez "
                     "d'autres modèles.")
NOT_SEPARATED_DIRECT = ("Les variantes sans raisonnement obtiennent presque toutes la même note : le jeu de test ne "
                        "permet pas de les départager. Le choix repose surtout sur le coût. Ajoutez des cas plus "
                        "difficiles ou comparez d'autres modèles.")
HIGH_SCORE = 0.98
HIGH_SCORE_SHARE = 0.80
SMALL_GAP = 0.03
MIN_VARIANTS_TO_COMPARE = 3


def not_separated(rows: list[Row], variants, threshold: float) -> list[str]:
    """One warning per message, naming the models whose variants all score about the same. Scores are not touched.

    Rule A: at least 80 % of a model's variants score 98 % or more. Rule B: among its variants without reasoning,
    the gap is at most 3 points or one case, and the best one reaches the threshold (otherwise cost decides nothing).
    """
    with_reasoning = {v.id for v in variants if v.reasoning}
    by_model: dict[str, list[Row]] = {}
    for r in rows:
        if r.n_cases > 0 and not r.unfair:
            by_model.setdefault(r.model, []).append(r)
    found: dict[str, list[str]] = {}
    for model, group in by_model.items():
        direct = [r.score for r in group if r.variant not in with_reasoning]
        n = max(r.n_cases for r in group)
        if (len(group) >= MIN_VARIANTS_TO_COMPARE
                and sum(r.score + EPS >= HIGH_SCORE for r in group) / len(group) + EPS >= HIGH_SCORE_SHARE):
            found.setdefault(NOT_SEPARATED_ALL, []).append(model)
        elif (len(direct) >= MIN_VARIANTS_TO_COMPARE and max(direct) + EPS >= threshold
                and max(direct) - min(direct) <= max(SMALL_GAP, 1 / n) + EPS):
            found.setdefault(NOT_SEPARATED_DIRECT, []).append(model)
    return [f"{', '.join(models)} : {message}" for message, models in found.items()]


# --- Score per tag (informative: selection never looks at it) ------------------------

def tag_scores(results: list[CallResult], cases: list[Case],
               config: Config) -> tuple[list[str], dict[tuple[str, str], dict[str, tuple[float, int]]]]:
    """(tags, (model, variant) -> tag -> (successes, cases)). No tag at all in the cases: nothing to show."""
    tag_of = {c.id: c.tag or UNTAGGED for c in cases}
    if not any(c.tag for c in cases):
        return [], {}
    expected = {c.id: c.expected for c in cases}
    tags = sorted(set(tag_of.values()) - {UNTAGGED}) + ([UNTAGGED] if UNTAGGED in tag_of.values() else [])
    groups: dict[tuple[str, str], list[CallResult]] = {}
    for r in results:
        if r.case in tag_of:
            groups.setdefault((r.model, r.variant), []).append(r)
    table = {}
    for couple, group in groups.items():
        cells: dict[str, tuple[float, int]] = {}
        for case, rate in case_rates(group, expected, config.scoring, config.fields).items():
            k, n = cells.get(tag_of[case], (0.0, 0))
            cells[tag_of[case]] = (k + rate, n + 1)
        table[couple] = cells
    return tags, table


# --- Validation --------------------------------------------------------------------

def validation_pairs(selection: Selection) -> list[tuple[str, str]]:
    """(model, variant) to validate: the winner and its near-ties. Empty when nothing was retained."""
    if selection.winner is None:
        return []
    return [(r.model, r.variant) for r in [selection.winner, *selection.near_ties]]


def validate(results: list[CallResult], expected: dict[str, str], selection: Selection,
             config: Config) -> list[Validation]:
    """Score the winner and its near-ties on the validation set.

    A candidate is confirmed when its raw score reaches the threshold: a Wilson lower bound cannot reach it
    on a small set (4 cases cap at about 51 %). The interval is still reported. The recommendation is never
    changed here: if the winner is not confirmed, nobody takes its place.
    """
    out = []
    for r in [selection.winner, *selection.near_ties] if selection.winner else []:
        group = [x for x in results if x.model == r.model and x.variant == r.variant and x.phase == "validation"]
        n, score, lo, hi = _scored(group, expected, config)
        planned = len({x.case for x in group}) or len(expected)
        out.append(Validation(r.model, r.variant, n, planned, score, lo, hi,
                              confirmed=n > 0 and score + EPS >= selection.threshold,
                              indicative=planned < INDICATIVE_BELOW, is_winner=r is selection.winner))
    return out


def winner_confirmed(validations: list[Validation]) -> bool | None:
    """True or False for the winner; None when there was no winner to validate."""
    return next((v.confirmed for v in validations if v.is_winner), None)


# --- Economy -------------------------------------------------------------------------

@dataclass(frozen=True)
class Economy:
    """The saving against the current prompt on the current model, or the reason there is no figure."""
    reference: Row | None            # BASE on current_model, when it could be measured
    savings: Savings | None
    reason: str | None               # why there is no figure; no assumption is ever made
    equal_quality: bool | None       # the reference itself demonstrates the threshold
    winner_is_reference: bool = False


def compute_economy(selection: Selection, config: Config) -> Economy:
    if not config.base_prompt:
        return Economy(None, None, "aucun base_prompt dans la configuration : il n'y a pas de référence à chiffrer.", None)
    if not config.current_model:
        return Economy(None, None, "current_model n'est pas renseigné : on ne sait pas sur quel modèle tourne le prompt "
                       "actuel, aucune économie n'est chiffrée.", None)
    ref = next((r for r in selection.rows if r.model == config.current_model and r.variant == BASE_ID), None)
    if ref is None or ref.cost_per_1000 is None:
        why = f" (statut : {ref.status})" if ref else ""
        return Economy(None, None, f"le prompt actuel n'a pas pu être mesuré sur {config.current_model}{why} : "
                       "aucune économie n'est chiffrée.", None)
    equal = ref.lo + EPS >= selection.threshold
    if selection.winner is None:
        return Economy(ref, None, "aucune variante n'est retenue : il n'y a rien à comparer à la référence.", equal)
    win = selection.winner
    return Economy(ref, savings(ref.cost_per_1000, win.cost_per_1000, config.monthly_volume), None, equal,
                   winner_is_reference=(win.model, win.variant) == (ref.model, ref.variant))
