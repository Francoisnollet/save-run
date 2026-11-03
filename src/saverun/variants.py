"""Prompt variants built from templates: combinations of four levers.

Levers: persona (none = control, generic, domain role), few-shot examples (0 or 3), step-by-step reasoning,
condensed instruction. Variant ids are stable: V01, V02... for a given config, cases file and seed.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from .config import Config
from .scoring import ANSWER_MARKER
from .splitting import Case

BASE_ID = "BASE"
N_SHOTS = 3

GENERIC_PERSONA = "Tu es un assistant précis."
LONG_SUFFIX = " Lis attentivement la demande et applique la consigne telle quelle."
FORMAT_EXACT = "Réponds uniquement par la réponse attendue, sans explication."
FORMAT_JSON = "Réponds uniquement par un objet JSON valide, sans autre texte."
REASONING = f"Réfléchis étape par étape, puis écris la réponse finale après « {ANSWER_MARKER} »."
# With an output_format, the reasoning instruction says where the format applies, so that a format such as
# "JSON only, no text before or after" cannot contradict "think first": it binds only what follows the marker.
REASONING_WITH_FORMAT = (
    "Réfléchis d'abord étape par étape, en écrivant ton raisonnement. "
    f"Puis écris « {ANSWER_MARKER} » suivi de ta réponse finale, au format demandé ci-dessus. "
    f"Ce format ne s'applique qu'à ce qui suit « {ANSWER_MARKER} » : ton raisonnement reste libre, "
    "et rien ne doit suivre la réponse.")


@dataclass(frozen=True)
class Variant:
    id: str
    description: str          # readable levers, shown in the table
    system: str               # the full prompt, ready to paste
    persona: str | None = None  # "aucun" | "générique" | "métier"; None for the reference prompt
    shots: int | None = None
    reasoning: bool | None = None
    concise: bool | None = None
    is_base: bool = False

    @property
    def levers(self) -> dict:
        return {"persona": self.persona, "exemples": self.shots, "raisonnement": self.reasoning,
                "condense": self.concise}


def _describe(persona: str, shots: int, reasoning: bool, concise: bool) -> str:
    return (f"persona={persona}, exemples={shots}, raisonnement={'oui' if reasoning else 'non'}, "
            f"condensé={'oui' if concise else 'non'}")


def render(config: Config, examples: list[Case], persona: str, shots: int, reasoning: bool,
           concise: bool) -> str:
    """Assemble one prompt from lever values."""
    parts = []
    if persona == "générique":
        parts.append(GENERIC_PERSONA)
    elif persona == "métier":
        parts.append(f"Tu es {config.role}.")
    parts.append(config.intention if concise else config.intention + LONG_SUFFIX)
    if shots:
        shown = "\n".join(f"Entrée : {e.input}\nSortie : {e.expected}" for e in examples[:shots])
        parts.append("Exemples :\n" + shown)
    if config.output_format:
        parts.append(config.output_format)      # replaces the generic format line, to avoid saying it twice
        if reasoning:
            parts.append(REASONING_WITH_FORMAT)
    elif reasoning:
        parts.append(REASONING)
    else:
        parts.append(FORMAT_JSON if config.scoring == "json_fields" else FORMAT_EXACT)
    return "\n\n".join(parts)


def build_variants(config: Config, examples: list[Case], n: int | None = None,
                   seed: int | None = None) -> tuple[list[Variant], list[str]]:
    """Return (variants, warnings). The plainest combination is always V01: it is the cheapest candidate
    and must never be shuffled out. The others are drawn with the seed, so the draw is reproducible."""
    n = config.n_variants if n is None else n
    seed = config.seed if seed is None else seed
    personas = ["aucun", "générique"] + (["métier"] if config.role else [])
    combos = list(itertools.product(personas, [0, N_SHOTS], [False, True], [False, True]))
    plain = ("aucun", 0, False, True)
    rest = [c for c in combos if c != plain]
    random.Random(seed).shuffle(rest)
    chosen = ([plain] + rest)[:n]

    warnings = []
    if n > len(combos):
        warnings.append(f"{n} variantes demandées mais seulement {len(combos)} combinaisons possibles"
                        + ("" if config.role else " (ajoutez `role` dans la config pour activer la persona métier)")
                        + f" : {len(combos)} variantes seront testées.")
    if len(examples) < N_SHOTS:
        raise ValueError(f"Il faut {N_SHOTS} exemples few-shot, {len(examples)} fournis.")
    variants = [Variant(id=f"V{i:02d}", description=_describe(*c), system=render(config, examples, *c),
                        persona=c[0], shots=c[1], reasoning=c[2], concise=c[3])
                for i, c in enumerate(chosen, start=1)]
    return variants, warnings


def base_variant(config: Config) -> Variant | None:
    """The current prompt, run as one more variant to price the saving. None without base_prompt."""
    if not config.base_prompt:
        return None
    return Variant(id=BASE_ID, description="prompt actuel (référence)", system=config.base_prompt, is_base=True)
