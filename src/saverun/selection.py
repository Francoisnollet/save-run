"""Selection by threshold on the Wilson lower bound, near-ties, and validation. Pure logic: no API call here.

Rules (see BRIEF.md): the unit is the case; the winner is the cheapest variant whose lower bound reaches the
threshold, never the best raw score; validation only confirms, it never re-selects.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .config import Config
from .cost import PriceTable, cost_per_1000
from .runner import MAX_FAILED_SHARE, CallResult, untested_models
from .scoring import case_rates, max_lower_bound, variant_score

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
        rows.append(Row(**base, n_cases=n, score=score, lo=lo, hi=hi, avg_in=avg_in, avg_out=avg_out,
                        cost_per_1000=cost_per_1000(avg_in, avg_out, price), failed_share=failed_share,
                        truncated_share=sum(r.truncated for r in done) / len(done)))
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
