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
git clone https://github.com/Francoisnollet/save-run.git save-run
cd save-run
pip install -e .
```

Premier essai, sans clé et sans dépense (faux modèle) :

```bash
saverun run --config config.example.yaml --cases cases.example.csv --dry-run
```

Les fichiers sont écrits dans `saverun-out/dry-run/`. Quand il se trompe, le faux modèle répond « indéterminé », une valeur hors de la liste attendue : l'avertissement « Réponses hors format » est donc normal dans cet essai.

Tests (sans réseau ni clé) :

```bash
pip install -e ".[dev]"
python -m pytest -q
```

## Fonctionnement

### 1. `saverun diagnose` : où part l'argent ?

À partir d'un export d'appels (CSV : `date, tache, modele, tokens_entree, tokens_sortie`), classe la dépense par tâche, projette un mois, et retient les tâches qui couvrent 80 % de la dépense.

```bash
saverun diagnose prototypes/calls.example.csv --prices prices.yaml   # export fictif fourni ; remplacez-le par le vôtre
```

La monnaie est celle de `prices.yaml`, sans conversion. Le `--seuil-interet` (100 par défaut, dans cette monnaie, par mois) est un **défaut arbitraire** : il indique la dépense sous laquelle l'optimisation ne vaut sans doute pas le temps passé. Fixez-le selon votre offre. Un modèle absent de `prices.yaml` est compté à part, jamais valorisé à zéro.

### 2. `saverun run` : trouver le couple le moins cher

```bash
cp config.example.yaml config.yaml     # puis adaptez-le
cp .env.example .env                   # puis renseignez vos clés
cp cases.example.csv cases.csv         # puis remplacez par vos cas
saverun run --cases cases.csv
```

1. Les cas (`cases.csv`, colonnes `input,expected`, et `tag` en option) sont séparés **avant toute exécution** (graine fixée) : 3 exemples few-shot, un ensemble de sélection, un ensemble de validation. Un cas n'est jamais dans deux ensembles. **Visez 50 cas** ; en dessous de 20, le résultat est indicatif.
2. Variantes par gabarit : persona (dont un témoin « aucun »), 0 ou 3 exemples, raisonnement, consigne condensée. Le tirage est équilibré (au moins la moitié des variantes sans raisonnement) et les exemples couvrent chaque valeur attendue quand c'est possible. Votre `base_prompt`, s'il est fourni, est testé comme variante de référence `BASE`.
3. Avant tout appel, SAVE Run affiche le nombre d'appels, un coût maximum estimé et la borne basse maximale atteignable avec vos cas, puis demande confirmation. Il redemande avant les appels de validation qui ne sont pas déjà faits, avec un coût maximum par candidat, même quand la sélection est entièrement reprise du cache. `max_calls` est respecté.
4. Notation déterministe (`exact` ou `json_fields`), puis note de chaque variante avec un intervalle de Wilson à 95 %. **L'unité statistique est le cas** : l'intervalle ne se resserre qu'avec plus de cas, pas avec `runs_per_case`.
5. Sélection : la moins chère dont la **borne basse** atteint le seuil. Les variantes qui le démontrent aussi, à moins de 10 % du coût de la retenue, sont signalées « quasi à égalité ».
6. Validation du gagnant et de ses quasi-égaux sur les cas mis de côté. Le gagnant est « confirmé » si sa **note brute** y atteint le seuil (un petit ensemble ne permet pas à la borne basse de l'atteindre ; l'intervalle est affiché quand même, et « indicatif » sous 20 cas). Sinon, le rapport dit « non confirmée » : aucun autre candidat n'est substitué, ce serait re-sélectionner sur la validation.

Fichiers produits dans `saverun-out/` : `results.csv` (une ligne par variante et par modèle, lisible par machine), `report.md` (assez propre pour être partagé tel quel ; `--no-footer` retire la ligne finale), `best_prompt.txt` et `best_config.json` (modèle, paramètres, exemples inclus), et `runs/` (sorties brutes : une relance ne repaie pas les appels déjà réussis).

La colonne `tag` (par exemple une famille de cas) ajoute au rapport une section « Note par tag ». Elle est informative : le tag n'est jamais envoyé au modèle, ne change ni la séparation des cas ni la sélection, et un avertissement signale un tag avec moins de 10 cas de sélection. Sans cette colonne, rien ne change. `tools/make_cases.py` accepte un fichier à 2 ou 3 colonnes (texte, oui/non, tag).

Options utiles : `--dry-run` (faux modèle, reproductible avec `--seed`), `--yes` (sans confirmation, ni avant la sélection ni avant la validation), `--out`, `--no-footer`.

### Choisir le seuil

Le seuil de qualité (`seuil_qualite`) est une **décision métier** : le même test peut donner un autre gagnant à 70 % et à 95 %, car un seuil plus exigeant écarte des couples moins chers.

Il dépend aussi du nombre de cas. Avec n cas de sélection, la borne basse maximale, atteinte avec une note de 100 %, est n/(n+3,84). Un seuil de 95 % demande donc au moins 73 cas de sélection, soit environ 100 cas au total avec `validation_part` à 0,25 et les 3 exemples few-shot.

### Configuration, prix, fournisseurs

- **`config.yaml`** : voir [config.example.yaml](config.example.yaml), commenté champ par champ. Aucun modèle n'est écrit en dur dans le code. `output_format` (optionnel) dit au modèle comment répondre et est ajouté à chaque variante générée, y compris sans exemple ; il est indispensable avec `json_fields` (sans lui, un avertissement s'affiche). `temperature` est optionnelle par modèle : absente, elle n'est jamais envoyée à l'API (le rapport indique « défaut du fournisseur »). Pour de l'extraction JSON, **augmentez `max_tokens`** (300 par défaut) : une réponse tronquée est notée comme les autres, donc le plus souvent fausse, et le rapport en donne la part par variante.
- **`prices.yaml`** : prix par million de tokens, avec `currency`, et une date et une source par modèle. Les valeurs fournies sont des **exemples fictifs, marqués « À VÉRIFIER »**. Un modèle absent du fichier bloque le lancement. Un modèle gratuit exige un tarif de référence, affiché comme « coût estimé ».
- **Fournisseurs** : `anthropic` et `openai_compatible` (URL de base configurable ; reportez-vous à la documentation de votre fournisseur pour l'URL et la limite de débit, rien n'est supposé). Limiteur de débit par modèle (`rpm`), nouvelle tentative avec attente croissante sur 429 et 5xx uniquement, appels en échec exclus de la note, et modèle arrêté après 3 refus de clé consécutifs (rapporté « non testé »).
- **Clés d'API** : uniquement par variable d'environnement ou fichier `.env` local (`.env.example` fourni). Elles ne sont jamais écrites dans le code, les journaux ni les fichiers de sortie.

Le moteur est importable (`saverun.pipeline.prepare` puis `execute_plan`), la ligne de commande est un fichier à part (`cli.py`).

## Limites connues

- **Testée sur quelques runs réels seulement** : deux tâches de classification (détection d'urgence sur e-mails synthétiques, et intentions bancaires sur le jeu public Banking77, voir ci-dessous) et trois modèles. Ces runs ne disent rien des autres tâches ni des autres fournisseurs, et les situations qu'ils n'ont pas rencontrées ne sont vérifiées qu'avec de faux clients.
- **Petits échantillons.** Avec peu de cas, les intervalles sont larges et le seuil peut être inatteignable (20 cas au total laissent 13 cas de sélection environ, soit une borne basse maximale d'environ 77 %). La validation sur peu de cas n'est qu'indicative. Visez 50 cas ou plus.
- **Petits écarts indépartageables.** Sur des écarts de quelques points, 88 cas de sélection ne permettent pas de départager des variantes : 100 % donne un intervalle de 96 à 100 % et 95,5 % un intervalle de 89 à 98 %, qui se chevauchent. Un écart plus net se voit : 90,9 % donne 83 à 95 %, sans chevauchement avec 100 %.
- **Toutes les erreurs comptent pareil.** Rater une urgence vaut autant que de la sur-détecter. Si l'une des deux erreurs vous coûte plus cher, la note ne le reflète pas.
- **Tâches à sortie vérifiable seulement.** La notation est déterministe (`exact`, `json_fields` tout-ou-rien) : pas de juge LLM, donc pas de textes libres, de résumés ni de rédaction.
- **Coût estimé, pas facture réelle.** Le coût est le nombre de tokens renvoyé par l'API multiplié par les prix de `prices.yaml`. Il ne tient compte ni des remises (cache, lots) ni d'un éventuel changement de tarif, et n'inclut pas le coût de l'exécution de SAVE Run elle-même (appels de test, réessais). Un modèle gratuit est valorisé à un tarif de référence : c'est un coût estimé, pas une économie réelle.
- **Prix à vérifier.** Les prix fournis sont des exemples fictifs. Vérifiez-les, avec leur date et leur source, avant de vous fier à un chiffre d'économie.
- **Variantes par gabarit seulement** : quatre leviers, gabarits en français ; pas de variantes générées par un LLM, pas d'arrêt anticipé, pas d'interface web.
- **L'économie suppose le même volume et le même type d'entrées** que vos cas de test.

### Essai sur un jeu public : Banking77

12 intentions proches tirées de Banking77 (6 sur les cartes, 6 sur les virements), 204 cas (3 exemples few-shot, 151 de sélection, 50 de validation), 8 variantes et le prompt de référence `BASE`, sur trois modèles : `claude-sonnet-4-5-20250929`, `claude-haiku-5-5` et `nvidia/nemotron-3.5-bolt-e4b`. Verdict selon le seuil, à partir des mêmes réponses :

| Seuil | Gagnant | Sélection : note (borne basse) | Validation (50 cas) |
|---|---|---|---|
| 90 % | aucun : la meilleure borne basse est 72 % | – | – |
| 70 % | V01 (sans persona ni exemples, sans raisonnement, consigne condensée) sur `claude-haiku-5-5` | 79 % (72 %) | 76 %, intervalle 63 % – 86 %, confirmé |
| 65 % | `BASE` sur `claude-haiku-5-5` | 75 % (68 %) | 76 %, intervalle 63 % – 86 %, confirmé |

Réserves :

- Le prompt `BASE` est **écrit par nous**, volontairement naïf (il liste les 12 étiquettes, sans définitions). Toute économie ou tout écart de qualité est mesuré contre ce prompt, pas contre un prompt réel d'entreprise. Les définitions des 12 étiquettes, dans l'intention, sont aussi écrites par nous, et figées avant le lancement.
- Cinq messages du jeu de test ont été lus par l'assistant de code : trois (intention `card_arrival`), en rédigeant les définitions ; ils ont servi à réécrire une définition et ont été exclus du tirage. Deux autres (même intention), affichés par erreur après la validation des définitions, n'ont pas été exclus.
- Banking77 est public et **probablement présent dans les données d'entraînement** des modèles testés.
- Les seuils de 70 % et 65 % ont été **choisis après avoir vu** qu'aucune variante ne démontrait 90 %. Ce ne sont pas des décisions métier.
- « Confirmé » veut dire que la note brute de validation atteint le seuil. Sur 50 cas, l'intervalle reste large (63 % – 86 %) : la validation ne démontre pas à elle seule le seuil.

Les notes ci-dessus sont celles obtenues avec la lecture tolérante du marqueur de réponse, ajoutée après le premier run : elle récupère 55 réponses justes mal lues (dont 44 pour une variante à raisonnement de Sonnet) et ne change aucun verdict.

Données : Casanueva, Temčinas, Gerz, Henderson et Vulić, « Efficient Intent Detection with Dual Sentence Encoders », *NLP for ConvAI*, ACL 2020, licence CC BY 4.0. Le jeu n'est pas redistribué dans ce dépôt.

## Licence

MIT, voir [LICENSE](LICENSE). Un projet [allrun](https://allrun.be).
