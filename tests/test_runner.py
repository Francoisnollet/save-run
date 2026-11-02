"""Runner tests: fake SDK clients and a fake model only, no network, no real key."""
from types import SimpleNamespace as NS

import pytest

from saverun.config import parse_config
from saverun.cost import MissingPriceError, ModelPrice, PriceTable
from saverun.fake import FakeClient
from saverun.runner import (AnthropicClient, CallResult, Completion, Job, MaxCallsError, MissingKeyError,
                            ModelRuntime, OpenAICompatibleClient, PermanentError, RateLimiter, RunStore,
                            TransientError, build_runtimes, classify_error, complete_with_retry, execute,
                            build_jobs, failure_stats, preflight, truncation_stats, unfair_variants,
                            untested_models)
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
    (SdkError(400), False), (SdkError(404), False), (ConnectionLost(), False), (ValueError("x"), False),
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
    assert str(e.value) == "limite de débit atteinte (HTTP 429)" and e.value.__cause__ is None


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
    runtimes = {"bon": ModelRuntime(Counting(), None), "mauvais": ModelRuntime(Counting(fail=True), None)}
    store = RunStore(tmp_path / "runs")
    res = execute(jobs_for(["bon", "mauvais"]), runtimes, store, sleep=lambda s: None)
    assert [r.model for r in res] == [j.model for j in jobs_for(["bon", "mauvais"])]  # job order kept
    assert all(r.ok for r in res if r.model == "bon")
    bad = [r for r in res if r.model == "mauvais"]
    assert all(r.status == "failed" and r.output is None and r.tokens_in is None for r in bad)
    assert "après 4 tentatives" in bad[0].error
    assert runtimes["mauvais"].client.n == 12 * 4          # every call tried 4 times, then given up


def test_failures_are_excluded_from_the_score_not_counted_wrong():
    expected = {"C001": "a", "C002": "a", "C003": "a"}

    def res(case, status, out):
        return CallResult("m", "V01", "h", case, "i", 0, "selection", None, 300, status, out)

    rates = case_rates([res("C001", "ok", "a"), res("C002", "failed", None), res("C003", "ok", "b")],
                       expected, "exact")
    assert rates == {"C001": 1.0, "C003": 0.0}  # C002 left out entirely, not scored 0


def test_failure_share_flags_variants_not_comparable():
    ok = lambda v: CallResult("m", v, "h", "c", "i", 0, "selection", None, 300, "ok", "x", 1, 1)
    ko = lambda v: CallResult("m", v, "h", "c", "i", 0, "selection", None, 300, "failed", error="e")
    stats = failure_stats([ok("V01")] * 10 + [ok("V02")] * 8 + [ko("V02")] * 2)
    assert stats[("m", "V02")].share == pytest.approx(0.2)
    assert unfair_variants(stats) == [("m", "V02")]
    assert unfair_variants(stats, max_share=0.25) == []


def test_resume_reuses_ok_calls_and_retries_failed_ones(tmp_path):
    store = RunStore(tmp_path)
    jobs = jobs_for(["m"], n_cases=3, variants=("V01",))
    c = Counting()
    execute(jobs, {"m": ModelRuntime(c, None)}, store, sleep=lambda s: None)
    assert c.n == 3
    c2 = Counting()
    res = execute(jobs, {"m": ModelRuntime(c2, None)}, store, sleep=lambda s: None)
    assert c2.n == 0 and all(r.ok for r in res)                      # nothing paid twice
    changed = [Job(j.model, j.variant, j.system + " modifié", j.case, j.input, j.run, j.phase) for j in jobs]
    execute(changed, {"m": ModelRuntime(c2, None)}, store, sleep=lambda s: None)
    assert c2.n == 3                                                  # a changed prompt is never reused

    broken = RunStore(tmp_path / "b")
    execute(jobs, {"m": ModelRuntime(Counting(fail=True), None)}, broken, sleep=lambda s: None)
    c3 = Counting()
    execute(jobs, {"m": ModelRuntime(c3, None)}, broken, sleep=lambda s: None)
    assert c3.n == 3                                                  # failed calls are tried again


def test_runs_files_never_contain_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SR_KEY", SECRET)

    def create(**kw):
        raise SdkError(429)

    client = AnthropicClient("m", "SR_KEY", sdk_client=NS(messages=NS(create=create)))
    store = RunStore(tmp_path / "runs")
    progress = []
    execute(jobs_for(["m"], 2, ("V01",)), {"m": ModelRuntime(client, None)}, store, progress=progress.append, sleep=lambda s: None)
    text = " ".join(p.read_text(encoding="utf-8") for p in (tmp_path / "runs").rglob("*") if p.is_file())
    assert text and SECRET not in text and "Bearer" not in text and "x-api-key" not in text
    assert SECRET not in repr(client) and len(progress) == 2


def test_truncated_last_line_is_ignored(tmp_path):
    store = RunStore(tmp_path)
    execute(jobs_for(["m"], 2, ("V01",)), {"m": ModelRuntime(Counting(), None)}, store, sleep=lambda s: None)
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"model": "m", "var')
    assert len(store.read_all()) == 2


# --- pre-launch check ---------------------------------------------------------------

PRICES = PriceTable("EUR", {"m": ModelPrice(1.0, 5.0, "2026-01-01", "s")})


def test_preflight_cost_by_hand_and_reuse(tmp_path):
    jobs = [Job("m", "V01", "x" * 380, f"C{i}", "y" * 20, 0, "selection") for i in range(10)]
    # 400 chars = 100 tokens in; 300 out: (100 x 1 + 300 x 5) / 1e6 = 0.0016 per call
    pf = preflight(jobs, PRICES, None)
    assert pf.new_calls == 10 and pf.max_cost == pytest.approx(0.016) and pf.currency == "EUR"
    store = RunStore(tmp_path)
    execute(jobs[:4], {"m": ModelRuntime(Counting(), None)}, store, sleep=lambda s: None)
    pf = preflight(jobs, PRICES, None, store)
    assert (pf.reused, pf.new_calls) == (4, 6) and pf.max_cost == pytest.approx(0.0096)


def test_preflight_blocks_on_missing_price_and_max_calls():
    jobs = jobs_for(["m", "inconnu"], 2, ("V01",))
    with pytest.raises(MissingPriceError):
        preflight(jobs, PRICES, None)
    with pytest.raises(MaxCallsError):
        preflight(jobs_for(["m"], 6), PRICES, 11)
    assert preflight(jobs_for(["m"], 6), PRICES, 12).new_calls == 12


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
    outs = {c.complete("Classe.", "u", max_tokens=100, temperature=None).text for _ in range(30)}
    assert outs == {"x", "indéterminé"}


def test_fake_failure_rate_exercises_retries_and_failures(tmp_path):
    c = FakeClient("a", {"u": "x"}, failure_rate=0.9, seed=3)
    res = execute([Job("a", "V01", "s", f"C{i}", "u", 0, "selection") for i in range(20)],
                  {"a": ModelRuntime(c, None)}, RunStore(tmp_path), sleep=lambda s: None)
    assert any(r.ok for r in res) and any(not r.ok for r in res)  # retries rescue some calls, others end "failed"


# --- adjustments after review: truncation, retry policy, reuse key -----------------------

def test_truncation_is_flagged_by_both_adapters():
    a = AnthropicClient("m", "K", sdk_client=NS(messages=NS(create=lambda **kw: NS(
        content=[NS(type="text", text="Je réfl")], stop_reason="max_tokens",
        usage=NS(input_tokens=5, output_tokens=300)))))
    assert a.complete("s", "u", max_tokens=300, temperature=None).truncated is True
    done = AnthropicClient("m", "K", sdk_client=NS(messages=NS(create=lambda **kw: NS(
        content=[NS(type="text", text="ok")], stop_reason="end_turn", usage=NS(input_tokens=5, output_tokens=1)))))
    assert done.complete("s", "u", max_tokens=300, temperature=None).truncated is False

    def create(finish):
        return lambda **kw: NS(choices=[NS(message=NS(content="{"), finish_reason=finish)],
                               usage=NS(prompt_tokens=5, completion_tokens=300))

    for finish, expected in (("length", True), ("stop", False)):
        o = OpenAICompatibleClient("m", "u", "K", sdk_client=NS(chat=NS(completions=NS(create=create(finish)))))
        assert o.complete("s", "u", max_tokens=300, temperature=None).truncated is expected


def test_truncated_answers_are_tallied_apart_and_still_scored(tmp_path):
    exp = {f"in{i}": "x" * 40 for i in range(4)}                    # a 10-token answer
    client = FakeClient("a", exp, seed=0)
    short = [Job("a", "V01", "Classe.", f"C{i}", f"in{i}", 0, "selection", None, 2) for i in range(4)]
    roomy = [Job("a", "V02", "Classe.", f"C{i}", f"in{i}", 0, "selection", None, 300) for i in range(4)]
    res = execute(short + roomy, {"a": ModelRuntime(client, None)}, RunStore(tmp_path), sleep=lambda s: None)
    stats = truncation_stats(res)
    assert stats[("a", "V01")].share == 1.0 and stats[("a", "V02")].share == 0.0
    assert all(r.ok and r.truncated for r in res if r.variant == "V01")  # completed, not failed
    rates = case_rates([r for r in res if r.variant == "V01"], {f"C{i}": "x" * 40 for i in range(4)}, "exact")
    assert rates == {f"C{i}": 0.0 for i in range(4)}                  # a cut answer is a wrong answer


@pytest.mark.parametrize("status,fragment", [
    (400, "requête refusée"), (401, "clé API refusée"), (403, "clé API refusée"), (404, "introuvable"),
])
def test_client_errors_fail_at_once_with_a_clear_message(tmp_path, status, fragment):
    calls = []

    def create(**kw):
        calls.append(1)
        raise SdkError(status)

    client = AnthropicClient("m", "K", sdk_client=NS(messages=NS(create=create)))
    slept = []
    res = execute(jobs_for(["m"], 2, ("V01",)), {"m": ModelRuntime(client, None)}, RunStore(tmp_path),
                  sleep=slept.append)
    assert len(calls) == 2 and slept == []                       # one attempt per call, no waiting
    assert all(r.status == "failed" and fragment in r.error and "tentatives" not in r.error for r in res)
    assert SECRET not in " ".join(r.error for r in res)


def test_missing_usage_fails_at_once_and_says_why(tmp_path):
    calls = []

    def create(**kw):
        calls.append(1)
        return NS(choices=[NS(message=NS(content="x"), finish_reason="stop")], usage=None)

    client = OpenAICompatibleClient("m", "u", "K", sdk_client=NS(chat=NS(completions=NS(create=create))))
    res = execute(jobs_for(["m"], 1, ("V01",)), {"m": ModelRuntime(client, None)}, RunStore(tmp_path),
                  sleep=lambda s: None)
    assert len(calls) == 1 and res[0].status == "failed"
    assert res[0].error == "l'API ne renvoie pas le nombre de tokens, SAVE Run ne peut pas chiffrer le coût"


def test_reuse_key_covers_model_prompt_case_run_temperature_and_max_tokens(tmp_path):
    base = Job("m", "V01", "prompt", "C001", "entrée", 0, "selection", None, 300)
    store = RunStore(tmp_path)
    c = Counting()
    execute([base], {"m": ModelRuntime(c, None), "m2": ModelRuntime(c, None)}, store, sleep=lambda s: None)
    assert c.n == 1
    variations = [
        Job("m2", "V01", "prompt", "C001", "entrée", 0, "selection", None, 300),    # model
        Job("m", "V01", "prompt", "C001", "entrée", 1, "selection", None, 300),     # run number
        Job("m", "V01", "prompt", "C001", "entrée", 0, "selection", 0.0, 300),      # temperature
        Job("m", "V01", "prompt", "C001", "entrée", 0, "selection", None, 600),     # max_tokens
        Job("m", "V01", "prompt", "C002", "entrée", 0, "selection", None, 300),     # case id
        Job("m", "V01", "prompt", "C001", "autre entrée", 0, "selection", None, 300),  # case input
        Job("m", "V01", "prompt 2", "C001", "entrée", 0, "selection", None, 300),   # prompt
    ]
    for v in variations:
        before = c.n
        execute([v], {"m": ModelRuntime(c, None), "m2": ModelRuntime(c, None)}, store, sleep=lambda s: None)
        assert c.n == before + 1, v
    before = c.n
    execute([base], {"m": ModelRuntime(c, None)}, store, sleep=lambda s: None)
    assert c.n == before  # the untouched call is still reused


def test_build_jobs_carries_temperature_and_max_tokens():
    cfg = config(runs_per_case=2, max_tokens=500)
    cases = [NS(id="C001", input="a"), NS(id="C002", input="b")]
    jobs = build_jobs([NS(id="V01", system="p")], cfg, cases, "selection")
    assert len(jobs) == 2 * 1 * 2 * 2                                # models x variants x cases x runs
    assert {(j.model, j.temperature, j.max_tokens) for j in jobs} == {("a", None, 500), ("b", 0.0, 500)}
    assert sorted({j.run for j in jobs}) == [0, 1]


# --- key refusals stop the model, not the run ----------------------------------------------

class Scripted:
    """Answers with a fixed script of outcomes: an int is an HTTP error status, anything else a success."""

    def __init__(self, script):
        self.script, self.n = list(script), 0

    def complete(self, system, user, *, max_tokens, temperature):
        step = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        if isinstance(step, int):
            raise classify_error(SdkError(step))
        return Completion("ok", 5, 1)


def test_three_consecutive_key_refusals_stop_only_that_model(tmp_path):
    bad, good = Scripted([401]), Scripted(["ok"])
    jobs = jobs_for(["bad", "good"], n_cases=6)                 # 12 jobs per model
    store = RunStore(tmp_path)
    res = execute(jobs, {"bad": ModelRuntime(bad, None), "good": ModelRuntime(good, None)}, store,
                  sleep=lambda s: None)
    assert bad.n == 3                                             # stopped: no more calls, no more waiting
    assert good.n == 12 and all(r.ok for r in res if r.model == "good")
    bads = [r for r in res if r.model == "bad"]
    assert [r.status for r in bads] == ["failed"] * 3 + ["skipped"] * 9
    assert "3 refus consécutifs" in bads[-1].error and "non testé" in bads[-1].error
    assert "clé API refusée" in bads[-1].error and SECRET not in bads[-1].error
    assert list(untested_models(res)) == ["bad"]
    assert all(r.status != "skipped" for r in store.read_all())   # skipped calls are not stored
    stats = failure_stats(res)
    assert stats[("bad", "V01")].total == 3 and stats[("good", "V01")].count == 0  # skipped never attempted


def test_a_success_resets_the_refusal_streak():
    c = Scripted([401, 403, "ok", 401, 401, "ok"])
    res = execute(jobs_for(["m"], n_cases=6, variants=("V01",)), {"m": ModelRuntime(c, None)},
                  _NullStore(), sleep=lambda s: None)
    assert c.n == 6 and not untested_models(res)
    assert [r.status for r in res] == ["failed", "failed", "ok", "failed", "failed", "ok"]


def test_other_client_errors_never_stop_a_model(tmp_path):
    c = Scripted([400])
    res = execute(jobs_for(["m"], n_cases=6, variants=("V01",)), {"m": ModelRuntime(c, None)},
                  RunStore(tmp_path), sleep=lambda s: None)
    assert c.n == 6 and all(r.status == "failed" for r in res) and not untested_models(res)


class _NullStore:
    def load_ok(self):
        return {}

    def append(self, result):
        pass
