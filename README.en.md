Version française : [README.md](README.md)

# SAVE Run

The tool's reports and messages are currently in French.

*Savings Against Value Erosion.* You have a fixed-prompt automation (classify, extract, route) that calls a language model at high volume. SAVE Run does not present itself as a prompt comparator: it delivers a **decision**.

- It generates variants of your prompt, runs them on one or more models, and scores each answer deterministically.
- It recommends the **cheapest (prompt, model) pair that demonstrates a quality threshold**: selection is based on the **lower bound** of the confidence interval, never on the best raw score.
- It confirms the choice on held-out cases, puts a figure on the cost per 1,000 calls, and delivers files ready to apply.

Good candidates: fixed prompt, high volume, automatically checkable output. Out of scope: open-ended assistants where everyone types their own question, and free text (summaries, writing), which would require an LLM-type judge.

## Example result

> **SIMULATION : faux modèle, résultats sans valeur.** (output is in French: "SIMULATION: fake model, worthless results.") **Fictitious** example, generated with `--dry-run`: no call was made, the prices are example values marked « À VÉRIFIER » ("to be checked"), and the fake model is tuned to illustrate the reasoning. These figures prove nothing.

Output is in French:

**Verdict.** V08 sur modele-exemple-anthropic est la moins chère à démontrer 75 % de qualité (note 100 %, borne basse 84 %), pour 0,163 EUR les 1 000 appels, confirmée sur les cas de validation.

| Variante | Modèle | Note | Intervalle 95 % | Coût / 1 000 appels (EUR) | Statut |
|---|---|---|---|---|---|
| V01 | modele-exemple-compatible | 25 % | 11 % – 47 % | 0,014 \* | sous le seuil |
| V05 | modele-exemple-compatible | 65 % | 43 % – 82 % | 0,016 \* | sous le seuil |
| V08 | modele-exemple-anthropic | 100 % | 84 % – 100 % | 0,163 | **retenue** |
| V20 | modele-exemple-anthropic | 90 % | 70 % – 97 % | 0,181 | sous le seuil |
| BASE (prompt actuel) | modele-exemple-anthropic | 100 % | 84 % – 100 % | 0,269 | seuil démontré, plus chère |

\* free model priced at a reference tariff: estimated cost. Extract of 5 rows out of 42.

How to read it: V20 has a raw score of 90%, but its lower bound (70%) does not demonstrate the 75% threshold; V08 demonstrates it, and is the cheapest to do so. The current prompt (`BASE`) demonstrates it too, but costs more. The full report (`report.md`) details the saving and the case-by-case comparison with the current prompt, the validation and the caveats.

## Installation

Python 3.11 or later.

```bash
git clone https://github.com/Francoisnollet/save-run.git save-run
cd save-run
pip install -e .
```

First try, with no key and no spending (fake model):

```bash
saverun run --config config.example.yaml --cases cases.example.csv --dry-run
```

The files are written to `saverun-out/dry-run/`. When it is wrong, the fake model answers « indéterminé » ("undetermined"), a value outside the expected list: the « Réponses hors format » warning ("off-format answers") is therefore normal in this try.

Tests (no network, no key):

```bash
pip install -e ".[dev]"
python -m pytest -q
```

## How it works

### 1. `saverun diagnose`: where does the money go?

From an export of calls (CSV: `date, tache, modele, tokens_entree, tokens_sortie`), it ranks spending by task, projects a month, and keeps the tasks that cover 80% of the spending.

```bash
saverun diagnose prototypes/calls.example.csv --prices prices.yaml   # fictitious export provided; replace it with yours
```

The currency is that of `prices.yaml`, with no conversion. `--seuil-interet` (100 by default, in that currency, per month) is an **arbitrary default**: it indicates the spending below which optimisation is probably not worth the time. Set it according to your offer. A model missing from `prices.yaml` is counted separately, never valued at zero.

### 2. `saverun run`: find the cheapest pair

```bash
cp config.example.yaml config.yaml     # then adapt it
cp .env.example .env                   # then fill in your keys
cp cases.example.csv cases.csv         # then replace with your cases
saverun run --cases cases.csv
```

1. The cases (`cases.csv`, columns `input,expected`, and optionally `tag` and `critical`) are split **before any execution** (fixed seed): 3 few-shot examples, a selection set, a validation set. A case is never in two sets. **Aim for 50 cases**; below 20, the result is indicative.
2. Template variants: persona (including an « aucun » ("none") control), 0 or 3 examples, reasoning, condensed instruction. The draw is balanced (at least half of the variants without reasoning) and the examples cover each expected value when possible. Your `base_prompt`, if provided, is tested as the reference variant `BASE`.
3. Before any call, SAVE Run displays the number of calls, an estimated maximum cost and the maximum lower bound reachable with your cases, then asks for confirmation. It asks again before the validation calls that have not already been made, with a maximum cost per candidate, even when the selection is entirely taken from the cache. With critical cases in the validation set, this quote has one more line: the current prompt run on those cases (see [Critical cases](#critical-cases-and-comparison-with-the-current-prompt)). `max_calls` is respected, these calls included.
4. Deterministic scoring (`exact` or `json_fields`), then a score for each variant with a 95% Wilson interval. **The statistical unit is the case**: the interval only narrows with more cases, not with `runs_per_case`.
5. Selection: the cheapest whose **lower bound** reaches the threshold. Variants that also demonstrate it, within 10% of the cost of the retained one, are flagged « quasi à égalité » ("near tie"). Possible statuses: « retenue » ("retained"), « quasi à égalité » ("near tie"), « seuil démontré, plus chère » ("threshold demonstrated, dearer"), « sous le seuil » ("below the threshold"), « casse un cas critique » ("breaks a critical case", see [Critical cases](#critical-cases-and-comparison-with-the-current-prompt)), « non testé » ("not tested", model stopped) and « aucun appel abouti » ("no completed call").
6. Validation of the winner and its near ties on the held-out cases. The winner is « confirmé » ("confirmed") if its **raw score** there reaches the threshold (a small set does not allow the lower bound to reach it; the interval is shown anyway, and « indicatif » ("indicative") below 20 cases) and, if there are critical cases in the validation set, if it does not break more of them than the tolerance. Otherwise, the report says « non confirmée » ("not confirmed"): no other candidate is substituted, as that would be re-selecting on the validation.

Files produced in `saverun-out/`: `results.csv` (one row per variant and per model, machine-readable), `comparison.csv` (case-by-case comparison with the current prompt, when there is a reference), `report.md` (clean enough to be shared as is; `--no-footer` removes the final line), `best_prompt.txt` and `best_config.json` (model, parameters, examples included), and `runs/` (raw outputs: a rerun does not pay again for calls that already succeeded).

The `tag` column (for example a family of cases) adds a « Note par tag » ("score by tag") section to the report. It is informative: the tag is never sent to the model, changes neither the split of the cases nor the selection, and a warning flags a tag with fewer than 10 selection cases. Without this column, nothing changes. `tools/make_cases.py` accepts a file with 2 or 3 columns (text, oui/non, tag).

Useful options: `--dry-run` (fake model, reproducible with `--seed`), `--yes` (no confirmation, neither before the selection nor before the validation), `--out`, `--no-footer`.

### Choosing the threshold

The quality threshold (`seuil_qualite`) is a **business decision**: the same test can give a different winner at 70% and at 95%, because a more demanding threshold rules out cheaper pairs.

It also depends on the number of cases. With n selection cases, the maximum lower bound, reached with a score of 100%, is n/(n+3.84). A 95% threshold therefore requires at least 73 selection cases, i.e. about 100 cases in total with `validation_part` at 0.25 and the 3 few-shot examples.

### Critical cases and comparison with the current prompt

A good average score can hide cases that the current prompt passed and that a cheaper variant fails. With `base_prompt` and `current_model`, SAVE Run therefore compares each variant with the current prompt, case by case, and you can mark the cases that must not be broken.

- **Reference**: `BASE` on `current_model`, the same as for the saving. Without either of them, no comparison is made and the report says why.
- **« Par rapport au prompt actuel » ("compared with the current prompt") section** of `report.md` and `comparison.csv` file: for each variant, the regressions (cases passed by the reference, failed by the variant), the corrections, the common failures and the unverified cases, on the selection cases. `comparison.csv` gives the case-by-case detail, and `results.csv` the same counts as columns. Both are produced for any user who has `base_prompt` and `current_model`, even without a `critical` column. This comparison alone does not change the selection.
- **Passed case**: a case is passed if its success rate over its runs reaches `success_rate` (0.6 by default; with `runs_per_case: 1`, if its answer is right). This parameter only serves the comparison and the critical cases: the score and the Wilson interval do not depend on it. A failed call on one side gives « non vérifié » ("unverified"), never a regression.
- **`critical` column** (optional) in `cases.csv`: `oui`, `true`, `1` or `yes` for a critical case; `non`, `false`, `0`, `no` or empty otherwise, case-insensitive. Any other value blocks the launch. Mark the critical cases **before** the run, not after seeing the results. Like `tag`, the column is never sent to the model and enters neither the reuse key nor the split of the cases: adding it redoes no selection call.
- **Selection**: a variant that would demonstrate the threshold but breaks more than `max_regressions_critiques` critical cases passed by the current prompt is set aside, with the status « casse un cas critique » ("breaks a critical case"), and the next cheapest wins. The default, 0, is strict: a single broken critical case sets the variant aside. The verdict names the cheapest variant set aside, its cost and its cases.
- **Validation**: the current prompt is also run on the critical validation cases, to compare the winner and its near ties with it. These calls appear in the confirmation quote, on a separate line, at the price of the reference model, and count towards `max_calls`. Without a critical case in the validation set, there is no extra call.
- **« Confirmé » ("confirmed")** then means: raw validation score at least equal to the threshold, **and** no more than `max_regressions_critiques` critical validation cases broken. Otherwise, « non confirmée » ("not confirmed"), with no substitution. A critical validation case that could not be compared (failed call, or calls of the current prompt declined at the quote) is named in the verdict, without withdrawing the confirmation.

Limitations:

- **Few critical cases, indicative result.** A warning is shown below 10 critical selection cases, below 5 validation ones, or if none fell in the validation set.
- **A critical case drawn as a few-shot example** is never evaluated; a warning names it.
- **Several runs per case: noise when the reference is exactly at the threshold.** If the reference passes a case only just (exactly `success_rate`), one run more or less is enough to create or erase a regression. The report counts these narrow passes.
- **Only what is marked critical is protected.** An important case that is not marked counts like the others.

### Configuration, prices, providers

- **`config.yaml`**: see [config.example.yaml](config.example.yaml), commented field by field. No model is hard-coded. `output_format` (optional) tells the model how to answer and is added to every generated variant, including those without examples; it is essential with `json_fields` (without it, a warning is shown). `temperature` is optional per model: when absent, it is never sent to the API (the report says « défaut du fournisseur », "provider default"). For JSON extraction, **increase `max_tokens`** (300 by default): a truncated answer is scored like the others, so most often wrong, and the report gives its share per variant.
- **`prices.yaml`**: price per million tokens, with `currency`, and a date and a source per model. The values provided are **fictitious examples, marked « À VÉRIFIER »**. A model missing from the file blocks the launch. A free model requires a reference tariff, shown as « coût estimé » ("estimated cost").
- **Providers**: `anthropic` and `openai_compatible` (configurable base URL; refer to your provider's documentation for the URL and the rate limit, nothing is assumed). Rate limiter per model (`rpm`), retry with increasing wait on 429 and 5xx only, failed calls excluded from the score, and model stopped after 3 consecutive key refusals (reported « non testé », "not tested").
- **API keys**: only through an environment variable or a local `.env` file (`.env.example` provided). They are never written in the code, the logs or the output files.

The engine is importable (`saverun.pipeline.prepare` then `execute_plan`), the command line is a separate file (`cli.py`).

## Known limitations

- **Tested on a few real runs only**: two classification tasks (urgency detection on synthetic e-mails, and banking intents on the public Banking77 dataset, see below) and three models. These runs say nothing about other tasks or other providers, and the situations they did not encounter are only checked with fake clients.
- **Small samples.** With few cases, the intervals are wide and the threshold may be unreachable (20 cases in total leave about 13 selection cases, i.e. a maximum lower bound of about 77%). Validation on few cases is only indicative. Aim for 50 cases or more.
- **Small gaps cannot be told apart.** On gaps of a few points, 88 selection cases cannot tell variants apart: 100% gives an interval of 96 to 100% and 95.5% an interval of 89 to 98%, which overlap. A clearer gap shows: 90.9% gives 83 to 95%, with no overlap with 100%.
- **All errors count the same.** Missing an urgent message counts as much as over-detecting one. If one of the two errors costs you more, the score does not reflect it. Critical cases make it possible to forbid a variant from breaking certain cases, but they do not weight the score.
- **Tasks with checkable output only.** Scoring is deterministic (`exact`, all-or-nothing `json_fields`): no LLM judge, so no free text, summaries or writing.
- **Estimated cost, not an actual bill.** The cost is the number of tokens returned by the API multiplied by the prices in `prices.yaml`. It takes into account neither discounts (cache, batches) nor any change of tariff, and does not include the cost of running SAVE Run itself (test calls, retries). A free model is valued at a reference tariff: it is an estimated cost, not an actual saving.
- **Prices to be checked.** The prices provided are fictitious examples. Check them, with their date and source, before relying on a saving figure.
- **Template variants only**: four levers, templates in French; no LLM-generated variants, no early stopping, no web interface.
- **The saving assumes the same volume and the same type of inputs** as your test cases.

### Trial on a public dataset: Banking77

12 close intents taken from Banking77 (6 about cards, 6 about transfers), 204 cases (3 few-shot examples, 151 for selection, 50 for validation), 8 variants and the reference prompt `BASE`, on three models: `claude-sonnet-4-5-20250929`, `claude-haiku-5-5` and `nvidia/nemotron-3.5-bolt-e4b`. Verdict by threshold, from the same answers:

| Threshold | Winner | Selection: score (lower bound) | Validation (50 cases) |
|---|---|---|---|
| 90% | none: the best lower bound is 72% | – | – |
| 70% | V01 (no persona or examples, no reasoning, condensed instruction) on `claude-haiku-5-5` | 79% (72%) | 76%, interval 63% – 86%, confirmed |
| 65% | `BASE` on `claude-haiku-5-5` | 75% (68%) | 76%, interval 63% – 86%, confirmed |

Caveats:

- The `BASE` prompt was **written by us**, deliberately naive (it lists the 12 labels, without definitions). Any saving or quality gap is measured against this prompt, not against a real company prompt. The definitions of the 12 labels, in the intent, were also written by us, and frozen before the launch.
- Five messages from the test set were read by the coding assistant: three (intent `card_arrival`), while writing the definitions; they were used to rewrite a definition and were excluded from the draw. Two others (same intent), displayed by mistake after the definitions were validated, were not excluded.
- Banking77 is public and **probably present in the training data** of the models tested.
- The 70% and 65% thresholds were **chosen after seeing** that no variant demonstrated 90%. They are not business decisions.
- "Confirmed" means that the raw validation score reaches the threshold (this test has no critical cases). On 50 cases, the interval remains wide (63% – 86%): the validation does not by itself demonstrate the threshold.

The scores above are those obtained with the tolerant reading of the answer marker, added after the first run: it recovers 55 correct answers that had been misread (44 of them for a reasoning variant on Sonnet) and changes no verdict.

Data: Casanueva, Temčinas, Gerz, Henderson and Vulić, "Efficient Intent Detection with Dual Sentence Encoders", *NLP for ConvAI*, ACL 2020, license CC BY 4.0. The dataset is not redistributed in this repository.

## License

MIT, see [LICENSE](LICENSE). An [allrun](https://allrun.be) project.
