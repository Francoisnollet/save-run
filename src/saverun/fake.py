"""Fake model for --dry-run: reproducible, free, and wired like a real adapter.

Its success rate depends on cues in the prompt (examples, step-by-step reasoning, persona), tuned so that
the pipeline and the reasoning behind a decision can be shown. Its numbers prove nothing about real models.
"""
from __future__ import annotations

import random
import zlib
from collections import defaultdict

from .cost import estimate_tokens
from .runner import Completion, TransientError

# Cues the fake model looks for in the prompt; variants.py must keep producing them.
EXAMPLES_CUE = "Exemples"
REASONING_CUE = "étape par étape"
PERSONA_CUE = "Tu es"
FINAL_MARKER = "Réponse :"

_MODEL_SHIFT = (-0.10, 0.0, 0.10)  # makes models differ in a multi-model dry run


class FakeClient:
    def __init__(self, model_id: str, expectations: dict[str, str], scoring: str = "exact",
                 seed: int = 0, failure_rate: float = 0.0):
        self.model_id = model_id
        self._expected = expectations          # case input -> expected value
        self._scoring = scoring
        self._seed = seed
        self._failure_rate = failure_rate
        self._shift = _MODEL_SHIFT[zlib.crc32(model_id.encode()) % 3]
        self._calls: dict[tuple[str, str], int] = defaultdict(int)
        self._fail_rng = random.Random(seed ^ 0x5EED)

    def complete(self, system: str, user: str, *, max_tokens: int, temperature: float | None) -> Completion:
        if self._failure_rate and self._fail_rng.random() < self._failure_rate:
            raise TransientError("FakeError (HTTP 503)")
        n = self._calls[(system, user)]        # the nth call on the same prompt and input differs (runs_per_case)
        self._calls[(system, user)] = n + 1
        rnd = random.Random(zlib.crc32((system + user).encode()) + self._seed + 1000 * n + self._shift_key())
        p = (0.55 + 0.15 * (EXAMPLES_CUE in system) + 0.10 * (REASONING_CUE in system)
             + 0.05 * (PERSONA_CUE in system) + self._shift)
        p = min(max(p, 0.05), 0.98)
        expected = self._expected[user]
        if rnd.random() < p:
            answer = expected
        else:
            answer = '{"erreur": true}' if self._scoring == "json_fields" else "indéterminé"
        text = f"Je réfléchis... {FINAL_MARKER} {answer}" if REASONING_CUE in system else answer
        return Completion(text, estimate_tokens(system + user), estimate_tokens(text))

    def _shift_key(self) -> int:
        return zlib.crc32(self.model_id.encode())
