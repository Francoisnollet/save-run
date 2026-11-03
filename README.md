# SAVE Run

*Savings Against Value Erosion.* Vous avez une automatisation à prompt fixe (classer, extraire, router) qui appelle un modèle de langage à fort volume. SAVE Run ne se présente pas comme un comparateur de prompts : il rend une **décision**.

- Il génère des variantes de votre prompt, les exécute sur un ou plusieurs modèles, et note chaque réponse de façon déterministe.
- Il recommande le couple **(prompt, modèle) le moins cher qui démontre un seuil de qualité** : la sélection se fait sur la **borne basse** de l'intervalle de confiance, jamais sur la meilleure note brute.
- Il confirme le choix sur des cas mis de côté, chiffre le coût pour 1 000 appels, et livre des fichiers prêts à appliquer.

Bonnes candidates : prompt fixe, volume élevé, sortie vérifiable automatiquement. Hors périmètre : assistants ouverts où chacun écrit sa question, et textes libres (résumés, rédaction), qui demanderaient un juge de type LLM.

## Exemple de résultat

> **SIMULATION : faux modèle, résultats sans valeur.** Exemple **fictif**, généré avec `--dry-run` : aucun appel n'a été fait, les prix sont des valeurs d'exemple « À VÉRIFIER », et le faux modèle est réglé pour illustrer le raisonnement. Ces chiffres ne prouvent rien.

**Verdict.** V08 sur modele-exemple-anthropic est la moins chère à démontrer 75 % de qualité (note 100 %, borne basse 84 %), pour 0,163 EUR les 1 000 appels, confirmée sur les cas de validation.

| Variante | Modèle | Note | Intervalle 95 % | Coût / 1 000 appels (EUR) | Statut |
|---|---|---|---|---|---|
| V01 | modele-exemple-compatible | 25 % | 11 % – 47 % | 0,014 \* | sous le seuil |
| V05 | modele-exemple-compatible | 65 % | 43 % – 82 % | 0,016 \* | sous le seuil |
| V08 | modele-exemple-anthropic | 100 % | 84 % – 100 % | 0,163 | **retenue** |
| V20 | modele-exemple-anthropic | 90 % | 70 % – 97 % | 0,181 | sous le seuil |
| BASE (prompt actuel) | modele-exemple-anthropic | 100 % | 84 % – 100 % | 0,269 | seuil démontré, plus chère |

\* modèle gratuit valorisé à un tarif de référence : coût estimé. Extrait de 5 lignes sur 42.

Lecture : V20 a une note brute de 90 %, mais sa borne basse (70 %) ne démontre pas le seuil de 75 % ; V08 le démontre, et c'est la moins chère à le faire. Le prompt actuel (`BASE`) le démontre aussi, mais coûte plus. Le rapport complet (`report.md`) détaille l'économie par rapport au prompt actuel, la validation et les réserves.

## Installation

Python 3.11 ou plus.

```bash
git clone <url-du-dépôt> save-run
cd save-run
pip install -e .
```

Premier essai, sans clé et sans dépense (faux modèle) :

```bash
saverun run --config config.example.yaml --cases cases.example.csv --dry-run
```

Les fichiers sont écrits dans `saverun-out/dry-run/`.

## Fonctionnement

### 1. `saverun diagnose` : où part l'argent ?

À partir d'un export d'appels (CSV : `date, tache, modele, tokens_entree, tokens_sortie`), classe la dépense par tâche, projette un mois, et retient les tâches qui couvrent 80 % de la dépense.

```bash
saverun diagnose appels.csv --prices prices.yaml
```

La monnaie est celle de `prices.yaml`, sans conversion. Le `--seuil-interet` (100 par défaut, dans cette monnaie, par mois) est un **défaut arbitraire** : il indique la dépense sous laquelle l'optimisation ne vaut sans doute pas le temps passé. Fixez-le selon votre offre. Un modèle absent de `prices.yaml` est compté à part, jamais valorisé à zéro.

### 2. `saverun run` : trouver le couple le moins cher

```bash
cp config.example.yaml config.yaml     # puis adaptez-le
cp .env.example .env                   # puis renseignez vos clés
saverun run --cases cases.csv
```

1. Les cas (`cases.csv`, colonnes `input,expected`) sont séparés **avant toute exécution** (graine fixée) : 3 exemples few-shot, un ensemble de sélection, un ensemble de validation. Un cas n'est jamais dans deux ensembles. **Visez 50 cas** ; en dessous de 20, le résultat est indicatif.
2. Variantes par gabarit : persona (dont un témoin « aucun »), 0 ou 3 exemples, raisonnement, consigne condensée. Votre `base_prompt`, s'il est fourni, est testé comme variante de référence `BASE`.
3. Avant tout appel, SAVE Run affiche le nombre d'appels, un coût maximum estimé et la borne basse maximale atteignable avec vos cas, puis demande confirmation. `max_calls` est respecté.
4. Notation déterministe (`exact` ou `json_fields`), puis note de chaque variante avec un intervalle de Wilson à 95 %. **L'unité statistique est le cas** : l'intervalle ne se resserre qu'avec plus de cas, pas avec `runs_per_case`.
5. Sélection : la moins chère dont la **borne basse** atteint le seuil. Les variantes qui le démontrent aussi, à moins de 10 % du coût de la retenue, sont signalées « quasi à égalité ».
6. Validation du gagnant et de ses quasi-égaux sur les cas mis de côté. Le gagnant est « confirmé » si sa **note brute** y atteint le seuil (un petit ensemble ne permet pas à la borne basse de l'atteindre ; l'intervalle est affiché quand même, et « indicatif » sous 20 cas). Sinon, le rapport dit « non confirmée » : aucun autre candidat n'est substitué, ce serait re-sélectionner sur la validation.

Fichiers produits dans `saverun-out/` : `results.csv` (une ligne par variante et par modèle, lisible par machine), `report.md` (assez propre pour être partagé tel quel ; `--no-footer` retire la ligne finale), `best_prompt.txt` et `best_config.json` (modèle, paramètres, exemples inclus), et `runs/` (sorties brutes : une relance ne repaie pas les appels déjà réussis).

Options utiles : `--dry-run` (faux modèle, reproductible avec `--seed`), `--yes` (sans confirmation), `--out`, `--no-footer`.

### Configuration, prix, fournisseurs

- **`config.yaml`** : voir [config.example.yaml](config.example.yaml), commenté champ par champ. Aucun modèle n'est écrit en dur dans le code. `temperature` est optionnelle par modèle : absente, elle n'est jamais envoyée à l'API (le rapport indique « défaut du fournisseur »). Pour de l'extraction JSON, **augmentez `max_tokens`** (300 par défaut) : une réponse tronquée est notée comme les autres, donc le plus souvent fausse, et le rapport en donne la part par variante.
- **`prices.yaml`** : prix par million de tokens, avec `currency`, et une date et une source par modèle. Les valeurs fournies sont des **exemples fictifs, marqués « À VÉRIFIER »**. Un modèle absent du fichier bloque le lancement. Un modèle gratuit exige un tarif de référence, affiché comme « coût estimé ».
- **Fournisseurs** : `anthropic` et `openai_compatible` (URL de base configurable ; reportez-vous à la documentation de votre fournisseur pour l'URL et la limite de débit, rien n'est supposé). Limiteur de débit par modèle (`rpm`), nouvelle tentative avec attente croissante sur 429 et 5xx uniquement, appels en échec exclus de la note, et modèle arrêté après 3 refus de clé consécutifs (rapporté « non testé »).
- **Clés d'API** : uniquement par variable d'environnement ou fichier `.env` local (`.env.example` fourni). Elles ne sont jamais écrites dans le code, les journaux ni les fichiers de sortie.

Le moteur est importable (`saverun.pipeline.prepare` puis `execute_plan`), la ligne de commande est un fichier à part (`cli.py`).

## Limites connues

- **La v0 n'a été testée qu'avec un faux modèle**, jusqu'au premier vrai run. Les adaptateurs `anthropic` et `openai_compatible` ne l'ont été qu'avec de faux clients : le comportement réel des API (comptage des tokens, motifs d'arrêt, limites de débit) n'a pas encore été observé.
- **Petits échantillons.** Avec peu de cas, les intervalles sont larges et le seuil peut être inatteignable (20 cas au total laissent 13 cas de sélection environ, soit une borne basse maximale d'environ 77 %). La validation sur peu de cas n'est qu'indicative. Visez 50 cas ou plus.
- **Tâches à sortie vérifiable seulement.** La notation est déterministe (`exact`, `json_fields` tout-ou-rien) : pas de juge LLM, donc pas de textes libres, de résumés ni de rédaction.
- **Coût estimé, pas facture réelle.** Le coût est le nombre de tokens renvoyé par l'API multiplié par les prix de `prices.yaml`. Il ne tient compte ni des remises (cache, lots) ni d'un éventuel changement de tarif, et n'inclut pas le coût de l'exécution de SAVE Run elle-même (appels de test, réessais). Un modèle gratuit est valorisé à un tarif de référence.
- **Prix à vérifier.** Les prix fournis sont des exemples fictifs. Vérifiez-les, avec leur date et leur source, avant de vous fier à un chiffre d'économie.
- **Variantes par gabarit seulement** : quatre leviers, gabarits en français ; pas de variantes générées par un LLM, pas d'arrêt anticipé, pas d'interface web.
- **L'économie suppose le même volume et le même type d'entrées** que vos cas de test.

## Licence

MIT, voir [LICENSE](LICENSE). Un projet [allrun](https://allrun.ai).
