"""Runner tests: fake SDK clients and a fake model only, no network, no real key."""
from types import SimpleNamespace as NS

import pytest

from saverun.config import parse_config
from saverun.cost import MissingPriceError, ModelPrice, PriceTable
from saverun.fake import FakeClient
from saverun.runner import (AnthropicClient, CallResult, Completion, Job, MaxCallsError, MissingKeyError,
                            ModelRuntime, OpenAICompatibleClient, PermanentError, RateLimiter, RunStore,
                            TransientError, build_runtimes, classify_error, complete_with_retry, execute,
                            failure_stats, preflight, unfair_variants)
from saverun.scoring import case_rates

SECRET = "sk-SECRET-0123456789"


class SdkError(Exception):
    """Mimics an SDK error whose text echoes request details, key included."""

    def __init__(self, status=None):
        super().__init__(f"boom Authorization: Bearer {SECRET} x-api-key: {SECRET}")
        self.status_code = status


class ConnectionLost(SdkError):
    pass


ConnectionLost.__name__ = "APIConnectionError"


# --- error classification and adapters ---------------------------------------------

@pytest.mark.parametrize("exc,transient", [
    (SdkError(429), True), (SdkError(503), True), (SdkError(500), True), (SdkError(401), False),
    (SdkError(400), False), (SdkError(404), False), (ConnectionLost(), True), (ValueError("x"), False),
])
def test_classify_error(exc, transient):
    out = classify_error(exc)
    assert isinstance(out, TransientError if transient else PermanentError)
    assert SECRET not in str(out) and "Bearer" not in str(out)


def test_anthropic_adapter_sends_temperature_only_when_set():
    calls = []

    def create(**kw):
        calls.append(kw)
        return NS(content=[NS(type="text", text="facturation")], usage=NS(input_tokens=12, output_tokens=3))

    c = AnthropicClient("m", "K", sdk_client=NS(messages=NS(create=create)))
    out = c.complete("sys", "usr", max_tokens=50, temperature=None)
    assert out == Completion("facturation", 12, 3)
    assert "temperature" not in calls[0] and calls[0]["system"] == "sys" and calls[0]["max_tokens"] == 50
    c.complete("sys", "usr", max_tokens=50, temperature=0.0)
    assert calls[1]["temperature"] == 0.0  # an explicit 0 is sent


def test_openai_adapter_uses_reported_tokens():
    calls = []

    def create(**kw):
        calls.append(kw)
        return NS(choices=[NS(message=NS(content="technique"))], usage=NS(prompt_tokens=20, completion_tokens=2))

    c = OpenAICompatibleClient("m", "https://exemple.invalid/v1", "K", sdk_client=NS(chat=NS(completions=NS(create=create))))
    assert c.complete("s", "u", max_tokens=10, temperature=None) == Completion("technique", 20, 2)
    assert "temperature" not in calls[0]
    assert [m["role"] for m in calls[0]["messages"]] == ["system", "user"]


def test_openai_adapter_refuses_to_invent_missing_usage():
    create = lambda **kw: NS(choices=[NS(message=NS(content="x"))], usage=None)
    c = OpenAICompatibleClient("m", "u", "K", sdk_client=NS(chat=NS(completions=NS(create=create))))
    with pytest.raises(PermanentError):
        c.complete("s", "u", max_tokens=10, temperature=None)


def test_adapter_errors_carry_no_sdk_text():
    def create(**kw):
        raise SdkError(429)

    c = AnthropicClient("m", "K", sdk_client=NS(messages=NS(create=create)))
    with pytest.raises(TransientError) as e:
        c.complete("s", "u", max_tokens=1, temperature=None)
    assert str(e.value) == "SdkError (HTTP 429)" and e.value.__cause__ is None


# --- rate limiter and retries ------------------------------------------------------

def test_rate_limiter_spaces_calls():
    now, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s

    lim = RateLimiter(60, clock=lambda: now[0], sleep=sleep)  # one call per second
    lim.wait()
    lim.wait()
    lim.wait()
    assert slept == [1.0, 1.0]
    now[0] += 10
    lim.wait()
    assert slept == [1.0, 1.0]  # idle time earns no debt and no wait


class Flaky:
    def __init__(self, failures, error=TransientError):
        self.failures, self.error, self.n = failures, error, 0

    def complete(self, system, user, *, max_tokens, temperature):
        self.n += 1
        if self.n <= self.failures:
            raise self.error("SdkError (HTTP 429)")
        return Completion("ok", 1, 1)


def test_retry_waits_grow_then_succeed():
    slept = []
    c = Flaky(2)
    assert complete_with_retry(c, None, "s", "u", max_tokens=1, temperature=None, sleep=slept.append).text == "ok"
    assert slept == [2.0, 4.0] and c.n == 3


def test_retry_is_bounded():
    slept, c = [], Flaky(99)
    with pytest.raises(TransientError):
        complete_with_retry(c, None, "s", "u", max_tokens=1, temperature=None, sleep=slept.append)
    assert c.n == 4 and slept == [2.0, 4.0, 8.0]


def test_permanent_error_is_not_retried():
    c = Flaky(99, PermanentError)
    with pytest.raises(PermanentError):
        complete_with_retry(c, None, "s", "u", max_tokens=1, temperature=None, sleep=lambda s: None)
    assert c.n == 1


# --- execution, failures, storage ---------------------------------------------------

def jobs_for(models, n_cases=6, variants=("V01", "V02")):
    return [Job(m, v, f"prompt {v}", f"C{i:03d}", f"entrée {i}", 0, "selection")
            for m in models for v in variants for i in range(1, n_cases + 1)]


class Counting:
    def __init__(self, fail=False):
        self.n, self.fail = 0, fail

    def complete(self, system, user, *, max_tokens, temperature):
        self.n += 1
        if self.fail:
            raise TransientError("SdkError (HTTP 503)")
        return Completion("ok", 10, 2)


def test_failed_calls_are_marked_and_do_not_stop_the_rest(tmp_path):
    runtimes = {"bon": ModelRuntime(Counting(), None, None), "mauvais": ModelRuntime(Counting(fail=True), None, None)}
    store = RunStore(tmp_path / "runs")
    res = execute(jobs_for(["bon", "mauvais"]), runtimes, store, max_tokens=10, sleep=lambda s: None)
    assert [r.model for r in res] == [j.model for j in jobs_for(["bon", "mauvais"])]  # job order kept
    assert all(r.ok for r in res if r.model == "bon")
    bad = [r for r in res if r.model == "mauvais"]
    assert all(r.status == "failed" and r.output is None and r.tokens_in is None for r in bad)
    assert "après 4 tentatives" in bad[0].error
    assert runtimes["mauvais"].client.n == 12 * 4          # every call tried 4 times, then given up


def test_failures_are_excluded_from_the_score_not_counted_wrong():
    expected = {"C001": "a", "C002": "a", "C003": "a"}

    def res(case, status, out):
        return CallResult("m", "V01", "h", case, "i", 0, "selection", status, out)

    rates = case_rates([res("C001", "ok", "a"), res("C002", "failed", None), res("C003", "ok", "b")],
                       expected, "exact")
    assert rates == {"C001": 1.0, "C003": 0.0}  # C002 left out entirely, not scored 0


def test_failure_share_flags_variants_not_comparable():
    ok = lambda v: CallResult("m", v, "h", "c", "i", 0, "selection", "ok", "x", 1, 1)
    ko = lambda v: CallResult("m", v, "h", "c", "i", 0, "selection", "failed", error="e")
    stats = failure_stats([ok("V01")] * 10 + [ok("V02")] * 8 + [ko("V02")] * 2)
    assert stats[("m", "V02")].share == pytest.approx(0.2)
    assert unfair_variants(stats) == [("m", "V02")]
    assert unfair_variants(stats, max_share=0.25) == []


def test_resume_reuses_ok_calls_and_retries_failed_ones(tmp_path):
    store = RunStore(tmp_path)
    jobs = jobs_for(["m"], n_cases=3, variants=("V01",))
    c = Counting()
    execute(jobs, {"m": ModelRuntime(c, None, None)}, store, max_tokens=10, sleep=lambda s: None)
    assert c.n == 3
    c2 = Counting()
    res = execute(jobs, {"m": ModelRuntime(c2, None, None)}, store, max_tokens=10, sleep=lambda s: None)
    assert c2.n == 0 and all(r.ok for r in res)                      # nothing paid twice
    changed = [Job(j.model, j.variant, j.system + " modifié", j.case, j.input, j.run, j.phase) for j in jobs]
    execute(changed, {"m": ModelRuntime(c2, None, None)}, store, max_tokens=10, sleep=lambda s: None)
    assert c2.n == 3                                                  # a changed prompt is never reused

    broken = RunStore(tmp_path / "b")
    execute(jobs, {"m": ModelRuntime(Counting(fail=True), None, None)}, broken, max_tokens=10, sleep=lambda s: None)
    c3 = Counting()
    execute(jobs, {"m": ModelRuntime(c3, None, None)}, broken, max_tokens=10, sleep=lambda s: None)
    assert c3.n == 3                                                  # failed calls are tried again


def test_runs_files_never_contain_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SR_KEY", SECRET)

    def create(**kw):
        raise SdkError(429)

    client = AnthropicClient("m", "SR_KEY", sdk_client=NS(messages=NS(create=create)))
    store = RunStore(tmp_path / "runs")
    progress = []
    execute(jobs_for(["m"], 2, ("V01",)), {"m": ModelRuntime(client, None, None)}, store, max_tokens=10,
            progress=progress.append, sleep=lambda s: None)
    text = " ".join(p.read_text(encoding="utf-8") for p in (tmp_path / "runs").rglob("*") if p.is_file())
    assert text and SECRET not in text and "Bearer" not in text and "x-api-key" not in text
    assert SECRET not in repr(client) and len(progress) == 2


def test_truncated_last_line_is_ignored(tmp_path):
    store = RunStore(tmp_path)
    execute(jobs_for(["m"], 2, ("V01",)), {"m": ModelRuntime(Counting(), None, None)}, store, max_tokens=1,
            sleep=lambda s: None)
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"model": "m", "var')
    assert len(store.read_all()) == 2


# --- pre-launch check ---------------------------------------------------------------

PRICES = PriceTable("EUR", {"m": ModelPrice(1.0, 5.0, "2026-01-01", "s")})


def test_preflight_cost_by_hand_and_reuse(tmp_path):
    jobs = [Job("m", "V01", "x" * 380, f"C{i}", "y" * 20, 0, "selection") for i in range(10)]
    # 400 chars = 100 tokens in; 300 out: (100 x 1 + 300 x 5) / 1e6 = 0.0016 per call
    pf = preflight(jobs, PRICES, 300, None)
    assert pf.new_calls == 10 and pf.max_cost == pytest.approx(0.016) and pf.currency == "EUR"
    store = RunStore(tmp_path)
    execute(jobs[:4], {"m": ModelRuntime(Counting(), None, None)}, store, max_tokens=300, sleep=lambda s: None)
    pf = preflight(jobs, PRICES, 300, None, store)
    assert (pf.reused, pf.new_calls) == (4, 6) and pf.max_cost == pytest.approx(0.0096)


def test_preflight_blocks_on_missing_price_and_max_calls():
    jobs = jobs_for(["m", "inconnu"], 2, ("V01",))
    with pytest.raises(MissingPriceError):
        preflight(jobs, PRICES, 300, None)
    with pytest.raises(MaxCallsError):
        preflight(jobs_for(["m"], 6), PRICES, 300, 11)
    assert preflight(jobs_for(["m"], 6), PRICES, 300, 12).new_calls == 12


# --- building clients ----------------------------------------------------------------

def config(**over):
    models = [{"id": "a", "provider": "anthropic", "api_key_env": "SR_KEY_A", "rpm": 10},
              {"id": "b", "provider": "openai_compatible", "base_url": "https://exemple.invalid/v1",
               "api_key_env": "SR_KEY_B", "rpm": 20, "temperature": 0}]
    return parse_config({"intention": "x", "models": models, **over})


def test_missing_keys_block_before_any_call(monkeypatch):
    monkeypatch.delenv("SR_KEY_A", raising=False)
    monkeypatch.setenv("SR_KEY_B", SECRET)
    with pytest.raises(MissingKeyError) as e:
        build_runtimes(config(), dry_run=False)
    assert "SR_KEY_A" in str(e.value) and SECRET not in str(e.value)


def test_dry_run_needs_no_key_and_no_rate_limit(monkeypatch):
    monkeypatch.delenv("SR_KEY_A", raising=False)
    rts = build_runtimes(config(), dry_run=True, expectations={"u": "x"})
    assert set(rts) == {"a", "b"} and all(r.limiter is None for r in rts.values())
    assert rts["a"].temperature is None and rts["b"].temperature == 0.0


# --- fake model ----------------------------------------------------------------------

def fake_answers(seed, system="Tu es un assistant.", model="a", n=40):
    exp = {f"in{i}": "facturation" for i in range(n)}
    c = FakeClient(model, exp, seed=seed)
    return [c.complete(system, u, max_tokens=10, temperature=None).text for u in exp]


def test_fake_is_reproducible_and_seed_changes_it():
    assert fake_answers(1) == fake_answers(1)
    assert fake_answers(1) != fake_answers(2)


def test_fake_cues_raise_the_success_rate():
    rate = lambda s: sum(t.endswith("facturation") for t in fake_answers(0, s, n=400)) / 400
    plain = rate("Classe.")
    assert rate("Classe.\n\nExemples :\nTicket : a\nCatégorie : b") > plain + 0.05


def test_fake_repeated_calls_differ_for_runs_per_case():
    c = FakeClient("a", {"u": "x"}, seed=0)
    outs = {c.complete("Classe.", "u", max_tokens=1, temperature=None).text for _ in range(30)}
    assert outs == {"x", "indéterminé"}


def test_fake_failure_rate_exercises_retries_and_failures(tmp_path):
    c = FakeClient("a", {"u": "x"}, failure_rate=0.9, seed=3)
    res = execute([Job("a", "V01", "s", f"C{i}", "u", 0, "selection") for i in range(20)],
                  {"a": ModelRuntime(c, None, None)}, RunStore(tmp_path), max_tokens=1, sleep=lambda s: None)
    assert any(r.ok for r in res) and any(not r.ok for r in res)  # retries rescue some calls, others end "failed"
