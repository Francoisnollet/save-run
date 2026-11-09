"""Deterministic scoring (exact, json_fields) and the Wilson confidence interval.

The statistical unit is the case: a case is either right or wrong for one run, its rate is the
mean over its runs, and the variant's score is the mean over cases, with a Wilson interval on n = number of cases.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

Z95 = 1.96
ANSWER_MARKER = "Réponse :"
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class ScoringError(ValueError):
    """Unusable `expected` value for the chosen scoring mode. User-facing (French)."""


@dataclass(frozen=True)
class Score:
    correct: bool
    detail: dict[str, bool] | None = None  # per-field result, json_fields only (informative)


# --- Wilson -------------------------------------------------------------------

def wilson(k: float, n: int, z: float = Z95) -> tuple[float, float]:
    """95 % Wilson interval for k successes out of n cases. k may be fractional (mean of case rates)."""
    if n <= 0:
        return 0.0, 0.0
    if not 0 <= k <= n:
        raise ValueError(f"k doit être entre 0 et n (k={k}, n={n}).")
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - margin), min(1.0, centre + margin)


def max_lower_bound(n: int, z: float = Z95) -> float:
    """Highest lower bound reachable with n cases (a perfect score): n / (n + z²)."""
    return wilson(n, n, z)[0]


def variant_score(case_rates: list[float]) -> tuple[float, float, float]:
    """(score, lower, upper) from the per-case success rates (each between 0 and 1)."""
    n = len(case_rates)
    if n == 0:
        return 0.0, 0.0, 0.0
    total = sum(case_rates)
    lo, hi = wilson(total, n)
    return total / n, lo, hi


# --- Output scoring -------------------------------------------------------------

def _norm(value: object) -> object:
    if isinstance(value, str):
        return value.strip().strip("\"'«»`").strip().rstrip(".").strip().casefold()
    return value


_SPACE = r"[ \t\u00a0\u202f]"   # incl. the no-break and narrow no-break spaces of French typography
_BOLD = r"(?:\*\*|__)"
# A tolerant marker, for reading only: « Réponse », « Answer », any case, at the start of a line (after optional
# spaces, a quote sign `>` or a heading `#`), optionally in bold, with or without a space before the colon.
# Mid-line it is not accepted, so a sentence such as "my answer: maybe" in the reasoning is never taken as the end.
_LINE_MARKER = re.compile(
    rf"^{_SPACE}*(?:>{_SPACE}*)?(?:#{{1,6}}{_SPACE}*)?{_BOLD}?(?:réponse|answer){_SPACE}*{_BOLD}?{_SPACE}*:{_BOLD}?",
    re.IGNORECASE | re.MULTILINE)
_WRAPPER = re.compile(rf"^{_BOLD}|{_BOLD}$")


def _marker_end(output: str) -> int | None:
    """End of the last answer marker: the exact ANSWER_MARKER anywhere, or a tolerant marker at a line start."""
    ends = [m.end() for m in _LINE_MARKER.finditer(output)]
    i = output.rfind(ANSWER_MARKER)
    if i >= 0:
        ends.append(i + len(ANSWER_MARKER))
    return max(ends) if ends else None


def extract_answer(output: str) -> str:
    """With step-by-step reasoning the final answer follows the last answer marker.

    The prompts always ask for ANSWER_MARKER; models also write « Réponse: », « Answer: » or a bold marker, which
    are read the same way. Bold around the answer is removed only after a marker: an output without any marker is
    returned unchanged, so a reply that ignores the requested format still counts as wrong.
    """
    end = _marker_end(output)
    if end is None:
        return output
    return _WRAPPER.sub("", output[end:].strip()).strip()


def parse_json_object(text: str) -> dict | None:
    """First JSON object found in a model output (tolerates code fences and surrounding prose)."""
    text = _FENCE.sub("", text.strip())
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _expected_object(expected: str) -> dict:
    try:
        obj = json.loads(expected)
    except ValueError:
        obj = None
    if not isinstance(obj, dict):
        raise ScoringError(f"« expected » n'est pas un objet JSON : {expected[:60]!r}")
    return obj


def expected_label(expected: str, scoring: str, fields: list[str] | None = None) -> str:
    """The value a case asks for, as a comparable string: exactly what scoring compares, nothing more.

    With json_fields only the listed fields count, so a free-text field never makes every case look unique.
    """
    if scoring == "json_fields":
        obj = _expected_object(expected)
        return json.dumps({f: _norm(obj[f]) for f in (fields or list(obj))}, sort_keys=True, ensure_ascii=False)
    return str(_norm(expected))


def validate_expected(expecteds: list[str], scoring: str, fields: list[str] | None = None) -> None:
    """Check every expected value before any call is made, so a bad file never wastes money."""
    if scoring == "exact":
        if any(not e.strip() for e in expecteds):
            raise ScoringError("Une valeur « expected » est vide.")
        return
    for e in expecteds:
        obj = _expected_object(e)
        missing = [f for f in (fields or []) if f not in obj]
        if missing:
            raise ScoringError(f"Champ(s) {', '.join(missing)} absent(s) de expected : {e[:60]!r}")


def score_output(output: str, expected: str, scoring: str, fields: list[str] | None = None) -> Score:
    if scoring == "exact":
        return Score(_norm(extract_answer(output)) == _norm(expected))
    if scoring == "json_fields":
        exp = _expected_object(expected)
        names = fields or list(exp)
        got = parse_json_object(extract_answer(output))
        detail = {f: got is not None and f in got and _norm(got[f]) == _norm(exp[f]) for f in names}
        return Score(all(detail.values()), detail)  # all-or-nothing; detail is informative only
    raise ValueError(f"scoring inconnu : {scoring}")


def case_rates(results, expected: dict[str, str], scoring: str, fields: list[str] | None = None) -> dict[str, float]:
    """Success rate per case over the runs that completed (duck-typed runner.CallResult list).

    A failed call is excluded, never counted as a wrong answer. A case whose every run failed is left out,
    so the interval then rests on fewer cases and widens accordingly.
    """
    per_case: dict[str, list[bool]] = {}
    for r in results:
        if r.status == "ok":
            per_case.setdefault(r.case, []).append(score_output(r.output, expected[r.case], scoring, fields).correct)
    return {case: sum(v) / len(v) for case, v in per_case.items()}
