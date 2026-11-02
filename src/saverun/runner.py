"""Model adapters, per-model rate limiting, retries, and raw-output storage in runs/.

Secrets: API keys are read from the environment, handed straight to the SDK constructor and never stored,
logged or written. Error messages keep only the exception class and the HTTP status, never the SDK text
(which may echo request details).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Protocol

from .config import Config, ModelConfig
from .cost import PriceTable, call_cost, estimate_tokens

MAX_ATTEMPTS = 4          # first try + 3 retries
BASE_DELAY = 2.0          # seconds, doubled after each failed attempt
MAX_FAILED_SHARE = 0.10   # above this share of failed calls, a variant is not fairly comparable

_NETWORK_ERRORS = {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout",
                   "ConnectionError", "TimeoutError"}


class TransientError(Exception):
    """Rate limit (429), server error (5xx) or network failure: worth retrying."""


class PermanentError(Exception):
    """Any other failure: retrying would not help."""


class MissingKeyError(RuntimeError):
    """An API-key environment variable is not set. Carries variable names only."""


class MaxCallsError(RuntimeError):
    """The run would exceed max_calls."""


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_in: int
    tokens_out: int


class ModelClient(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int, temperature: float | None) -> Completion: ...


# --- Adapters ---------------------------------------------------------------------

def classify_error(exc: Exception) -> Exception:
    """Map an SDK exception to Transient/PermanentError carrying only class name and HTTP status."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    label = f"{name} (HTTP {status})" if status else name
    transient = (status == 429 or (status or 0) >= 500
                 or (status is None and name in _NETWORK_ERRORS))
    return (TransientError if transient else PermanentError)(label)


def _read_key(env_name: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        raise MissingKeyError(f"Variable d'environnement absente : {env_name}")
    return value


class AnthropicClient:
    def __init__(self, model_id: str, api_key_env: str, sdk_client=None):
        self._model = model_id
        if sdk_client is None:
            import anthropic  # loaded only when a real model is called
            sdk_client = anthropic.Anthropic(api_key=_read_key(api_key_env), max_retries=0)
        self._sdk = sdk_client

    def complete(self, system, user, *, max_tokens, temperature):
        params = {} if temperature is None else {"temperature": temperature}
        try:
            r = self._sdk.messages.create(model=self._model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": user}], **params)
        except Exception as e:
            raise classify_error(e) from None
        text = "".join(b.text for b in r.content if getattr(b, "type", "text") == "text")
        return Completion(text, r.usage.input_tokens, r.usage.output_tokens)


class OpenAICompatibleClient:
    def __init__(self, model_id: str, base_url: str, api_key_env: str, sdk_client=None):
        self._model = model_id
        if sdk_client is None:
            import openai  # loaded only when a real model is called
            sdk_client = openai.OpenAI(api_key=_read_key(api_key_env), base_url=base_url, max_retries=0)
        self._sdk = sdk_client

    def complete(self, system, user, *, max_tokens, temperature):
        params = {} if temperature is None else {"temperature": temperature}
        try:
            r = self._sdk.chat.completions.create(
                model=self._model, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **params)
        except Exception as e:
            raise classify_error(e) from None
        usage = getattr(r, "usage", None)
        if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
            # Costs rest on tokens reported by the API: never estimate them for a real call.
            raise PermanentError("Réponse sans comptage de tokens (usage absent)")
        return Completion(r.choices[0].message.content or "", usage.prompt_tokens, usage.completion_tokens)


# --- Rate limiting and retries ----------------------------------------------------

class RateLimiter:
    """Spaces call starts by 60 / rpm seconds."""

    def __init__(self, rpm: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._interval = 60.0 / rpm
        self._next = 0.0
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            if start > now:
                self._sleep(start - now)
            self._next = start + self._interval


def complete_with_retry(client: ModelClient, limiter: RateLimiter | None, system: str, user: str, *,
                        max_tokens: int, temperature: float | None,
                        sleep: Callable[[float], None] = time.sleep,
                        max_attempts: int = MAX_ATTEMPTS, base_delay: float = BASE_DELAY) -> Completion:
    """Retry transient errors with growing waits; permanent errors and the last failure propagate."""
    for attempt in range(max_attempts):
        if limiter:
            limiter.wait()
        try:
            return client.complete(system, user, max_tokens=max_tokens, temperature=temperature)
        except TransientError:
            if attempt == max_attempts - 1:
                raise
            sleep(base_delay * 2 ** attempt)
    raise AssertionError("unreachable")


@dataclass
class ModelRuntime:
    client: ModelClient
    limiter: RateLimiter | None
    temperature: float | None  # None: not sent, the provider's default applies


def build_runtimes(config: Config, *, dry_run: bool, expectations: dict[str, str] | None = None,
                   seed: int | None = None) -> dict[str, ModelRuntime]:
    """One client per configured model. Checks every API key up front so nothing starts half-configured."""
    if dry_run:
        from .fake import FakeClient
        return {m.id: ModelRuntime(FakeClient(m.id, expectations or {}, config.scoring,
                                              config.seed if seed is None else seed), None, m.temperature)
                for m in config.models}
    absent = [m.api_key_env for m in config.models if not os.environ.get(m.api_key_env)]
    if absent:
        raise MissingKeyError("Variable(s) d'environnement absente(s) : " + ", ".join(sorted(set(absent)))
                              + ". Renseignez-les dans l'environnement ou dans un fichier .env local.")
    runtimes = {}
    for m in config.models:
        client = (AnthropicClient(m.id, m.api_key_env) if m.provider == "anthropic"
                  else OpenAICompatibleClient(m.id, m.base_url, m.api_key_env))
        runtimes[m.id] = ModelRuntime(client, RateLimiter(m.rpm), m.temperature)
    return runtimes


# --- Jobs, results, storage -------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Job:
    model: str
    variant: str
    system: str
    case: str
    input: str
    run: int
    phase: str  # "selection" or "validation"

    @property
    def key(self) -> tuple:
        """Identity of a call: same model, prompt, case input and run give a reusable result."""
        return (self.model, _sha(self.system), self.case, _sha(self.input), self.run)


@dataclass(frozen=True)
class CallResult:
    model: str
    variant: str
    prompt_hash: str
    case: str
    input_hash: str
    run: int
    phase: str
    status: str                  # "ok" or "failed"
    output: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def key(self) -> tuple:
        return (self.model, self.prompt_hash, self.case, self.input_hash, self.run)


class RunStore:
    """Append-only JSON lines in runs/. Lets a run be re-scored, or resumed, without calling the API again."""

    def __init__(self, directory: str | Path):
        self.path = Path(directory) / "calls.jsonl"
        self._lock = threading.Lock()

    def read_all(self) -> list[CallResult]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(CallResult(**json.loads(line)))
            except (ValueError, TypeError):
                continue  # a truncated last line after an interruption
        return out

    def load_ok(self) -> dict[tuple, CallResult]:
        return {r.key: r for r in self.read_all() if r.ok}

    def append(self, result: CallResult) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


def execute(jobs: list[Job], runtimes: dict[str, ModelRuntime], store: RunStore, *, max_tokens: int,
            progress: Callable[[CallResult], None] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> list[CallResult]:
    """Run every job; models run in parallel, each paced by its own rate limiter.

    A call that still fails after the retries is recorded as "failed" and the run goes on.
    Calls already stored with status "ok" are reused, not paid again. Results come back in job order.
    """
    reuse = store.load_ok()
    results: list[CallResult | None] = [None] * len(jobs)
    by_model: dict[str, list[int]] = {}
    for i, j in enumerate(jobs):
        by_model.setdefault(j.model, []).append(i)
    progress_lock = threading.Lock()

    def one(job: Job) -> CallResult:
        base = dict(model=job.model, variant=job.variant, prompt_hash=_sha(job.system), case=job.case,
                    input_hash=_sha(job.input), run=job.run, phase=job.phase)
        cached = reuse.get(job.key)
        if cached:
            return replace(cached, variant=job.variant, phase=job.phase)
        rt = runtimes[job.model]
        try:
            c = complete_with_retry(rt.client, rt.limiter, job.system, job.input, max_tokens=max_tokens,
                                    temperature=rt.temperature, sleep=sleep)
            res = CallResult(**base, status="ok", output=c.text, tokens_in=c.tokens_in, tokens_out=c.tokens_out)
        except (TransientError, PermanentError) as e:
            suffix = f", après {MAX_ATTEMPTS} tentatives" if isinstance(e, TransientError) else ""
            res = CallResult(**base, status="failed", error=f"{e}{suffix}")
        store.append(res)
        return res

    def worker(indexes: list[int]) -> None:
        for i in indexes:
            results[i] = one(jobs[i])
            if progress:
                with progress_lock:
                    progress(results[i])

    with ThreadPoolExecutor(max_workers=max(1, len(by_model))) as pool:
        for f in [pool.submit(worker, idx) for idx in by_model.values()]:
            f.result()  # propagates programming errors; API failures never get here
    return results  # type: ignore[return-value]


# --- Failures ---------------------------------------------------------------------

@dataclass(frozen=True)
class FailureStat:
    failed: int
    total: int

    @property
    def share(self) -> float:
        return self.failed / self.total if self.total else 0.0


def failure_stats(results: list[CallResult]) -> dict[tuple[str, str], FailureStat]:
    """Failed vs total calls per (model, variant)."""
    acc: dict[tuple[str, str], list[int]] = {}
    for r in results:
        a = acc.setdefault((r.model, r.variant), [0, 0])
        a[0] += not r.ok
        a[1] += 1
    return {k: FailureStat(*v) for k, v in acc.items()}


def unfair_variants(stats: dict[tuple[str, str], FailureStat],
                    max_share: float = MAX_FAILED_SHARE) -> list[tuple[str, str]]:
    """(model, variant) pairs with too many failed calls to be compared fairly with the others."""
    return [k for k, s in stats.items() if s.share > max_share]


# --- Pre-launch check -------------------------------------------------------------

@dataclass(frozen=True)
class Preflight:
    total_jobs: int
    reused: int
    new_calls: int
    calls_by_model: dict[str, int]
    max_cost: float          # upper estimate, in the price file's currency
    currency: str
    warnings: list[str] = field(default_factory=list)


def preflight(jobs: list[Job], prices: PriceTable, max_tokens: int, max_calls: int | None,
              store: RunStore | None = None) -> Preflight:
    """Count the calls to make and bound their cost. Blocks on a missing price or an exceeded max_calls."""
    models = sorted({j.model for j in jobs})
    prices.require(models)
    done = store.load_ok() if store else {}
    pending = [j for j in jobs if j.key not in done]
    if max_calls is not None and len(pending) > max_calls:
        raise MaxCallsError(f"{len(pending)} appels à faire, au-delà de max_calls ({max_calls}). "
                            "Réduisez n_variants, le nombre de modèles ou de cas, ou relevez max_calls.")
    by_model: dict[str, int] = {}
    cost = 0.0
    for j in pending:
        by_model[j.model] = by_model.get(j.model, 0) + 1
        cost += call_cost(estimate_tokens(j.system + j.input), max_tokens, prices.models[j.model])
    return Preflight(len(jobs), len(jobs) - len(pending), len(pending), by_model, cost, prices.currency,
                     prices.warnings(models))
