# Brief : SAVE Run v0 (version 2)

Nom de travail : SAVE Run (*Savings Against Value Erosion*). Dépôt suggéré : `save-run`.

## Objectif

Un outil en ligne de commande qui, pour une automatisation à **prompt fixe**, génère des variantes du prompt, les exécute sur un ou plusieurs modèles, les note, et recommande le couple (prompt, modèle) **le moins cher qui démontre un seuil de qualité**. Il chiffre l'économie et livre des fichiers prêts à appliquer.

## À qui il s'adresse

- **Bonne candidate** : prompt fixe, volume élevé, sortie vérifiable automatiquement (classer, extraire, router).
- **Hors périmètre** : assistants ouverts où chacun écrit sa question, et textes libres (résumés, rédaction), qui demandent un juge de type LLM.
- **Nuance** : les étapes internes d'un agent (reconnaître l'intention, router, extraire) sont des automatisations à prompt fixe, donc dans le périmètre.

## Open source et public visé

SAVE Run sera publié en open source pour faire connaître son auteur (marque allrun). Le dépôt reste privé pendant la construction, mais tout doit être écrit comme s'il allait devenir public.

- **Public principal** : équipes avec une automatisation à prompt fixe. **Public secondaire** : développeurs qui ont des appels LLM dans leur produit (classer, extraire, router). Pas les développeurs qui utilisent un agent pour coder : leurs prompts ne sont pas fixes.
- **Positionnement face aux outils connus** (Promptfoo, DSPy, etc.) : SAVE Run ne se présente pas comme un comparateur de prompts. Il rend une **décision** : le couple le moins cher qui démontre le seuil, avec intervalle de confiance et économie chiffrée. Le README le dit dès les premières lignes.
- **Licence** : MIT, fichier `LICENSE` à la racine.
- **Chaque utilisation fait connaître l'outil** : `report.md` se termine par une ligne sobre, supprimable par une option (`--no-footer`) : « Généré avec SAVE Run (https://allrun.be) ». Le rapport doit être assez propre pour être partagé tel quel.
- **README** : d'abord un exemple de résultat (le tableau et le verdict), puis l'installation en trois commandes, puis le fonctionnement. L'exemple est généré avec le faux modèle et **clairement indiqué comme fictif**.
- **Points d'extension pour les développeurs, v1** : installation par `pip`, configuration versionnée dans le dépôt, action GitHub qui relance le test quand un modèle ou un prix change. Ne pas les coder en v0, mais ne pas les empêcher : moteur importable, sorties lisibles par machine (`results.csv`, `best_config.json`).
- **Hygiène avant publication** : aucun secret ni donnée client dans l'historique git, `.gitignore` en place, données d'exemple fictives, section « Limites connues » honnête dans le README.

## Prototypes fournis (dossier `prototypes/`)

- `saverun.py` : prototype en un fichier, testé **uniquement avec un faux modèle** (`--dry-run`). Il contient la logique voulue : variantes par gabarit, intervalle de Wilson, sélection par seuil, égalités, `--runs`.
- `diagnostic.py` : diagnostic de facture à partir d'un export d'appels.
- `cases.example.csv`, `calls.example.csv`, `prix.example.csv` : données fictives.

À refactorer en modules, pas à recopier. Les chiffres produits par le faux modèle ne prouvent rien : il est réglé pour que les exemples et le raisonnement aident.

## Périmètre de la v0

**Dans la v0**
1. `saverun diagnose` : dépense par tâche à partir d'un export CSV d'appels (voir prototype).
2. `saverun run` : génération de variantes par gabarit, exécution, notation déterministe, coût, sélection par seuil, validation sur cas mis de côté, rapport et export.
3. `--dry-run` avec un faux modèle.

**Hors v0, par ordre de priorité pour la v1**
- Arrêt anticipé et premier tri rapide sur peu de cas (idée forte du projet : prévoir les points d'extension dès maintenant).
- Variantes générées par un LLM à partir de l'intention (méta-prompt), à la place des gabarits.
- Convertisseurs de journaux (OpenAI, Anthropic, n8n) vers le CSV du diagnostic.
- Récupération automatique des prix.
- Interface web, textes libres, juge LLM.

## Entrées

`config.yaml` :
- `intention` : ce que le prompt doit faire.
- `base_prompt` : optionnel, le prompt actuel (variante de référence pour chiffrer l'économie).
- `models` : liste ; chacun a `id`, `provider` (`anthropic` ou `openai_compatible`), `base_url` (pour `openai_compatible`), `api_key_env`, `rpm` (requêtes par minute). Jamais de modèle en dur dans le code.
- `n_variants` (20 par défaut), `runs_per_case` (1), `seuil_qualite` (0,75), `validation_part` (0,25), `max_calls`, `scoring` (`exact` ou `json_fields`), `monthly_volume` (optionnel, pour l'économie en euros).

`cases.csv` : colonnes `input`, `expected`. 20 cas au minimum, 50 recommandés. En dessous de 20, avertir que le résultat est indicatif.

## Pipeline

1. Séparer les cas **avant toute exécution** (graine fixée) : exemples few-shot, ensemble de sélection, ensemble de validation. Un cas n'est jamais dans deux ensembles.
2. Générer les variantes (leviers : persona dont un témoin « aucun », exemples 0 ou 3, raisonnement oui ou non, consigne condensée ou non). Identifiant stable `V01`…, description lisible.
3. Exécuter chaque variante sur chaque cas de sélection, pour chaque modèle ; enregistrer sortie et tokens renvoyés par l'API dans `runs/`.
4. Noter : taux de réussite du cas, puis note de la variante avec intervalle de Wilson à 95 %.
5. Sélectionner (règles ci-dessous).
6. Valider le gagnant, et ses quasi-égaux, sur l'ensemble de validation (critère ci-dessous : note brute, pas borne basse). Le gagnant est « confirmé » ou « non confirmée » ; le résultat des quasi-égaux est affiché sans changer la recommandation.
7. Produire le rapport et l'export.

## Règles statistiques (non négociables)

- **L'unité statistique est le cas.** L'intervalle ne se resserre qu'avec plus de cas.
- **Sélection** : la moins chère dont la **borne basse** de l'intervalle atteint le seuil. Jamais « la meilleure note brute ». Si aucune variante ne démontre le seuil, le dire et suggérer de baisser le seuil ou d'ajouter des cas.
- **Égalités** : les variantes qui démontrent aussi le seuil, à moins de 10 % du coût de la retenue, sont signalées comme quasi à égalité.
- **`runs_per_case`** : stabilise la note d'un cas, ne resserre pas l'intervalle. À température 0 il est inutile : avertir.
- **Atteignabilité du seuil** : avant le lancement, afficher « avec N cas de sélection, la borne basse maximale possible est X % » (une variante parfaite a pour borne basse `N / (N + 1,96²)`). Si X est sous le seuil, dire que le seuil est inatteignable et combien de cas il faut. Aucun défaut n'est modifié pour autant.
- **Validation** : elle ne sert qu'à confirmer ; ne jamais re-sélectionner sur l'ensemble de validation.
  - *Critère de confirmation* : le gagnant est « confirmé » si sa **note brute** sur l'ensemble de validation atteint le seuil. L'intervalle de Wilson est toujours affiché, mais il ne décide pas : sur un petit ensemble la borne basse ne peut pas atteindre le seuil (4 cas : environ 51 % au mieux ; 11 cas : environ 74 %), ce qui rendrait toute confirmation impossible. (Version précédente du brief : « la borne basse doit passer le seuil » ; abandonnée pour cette raison.)
  - *Ensemble de validation de moins de 20 cas* : le rapport indique « indicatif ».
  - *Gagnant non confirmé* : ne basculer sur aucun autre candidat (ce serait re-sélectionner sur la validation). Le rapport dit « non confirmée » et suggère d'ajouter des cas.
  - *Quasi-égaux* : validés aussi, résultats affichés, recommandation inchangée.

## Coût

- `prices.yaml` : prix par million de tokens en entrée et en sortie, par modèle, avec une **date** et une **source**.
- **Ne jamais inventer de prix.** Valeurs d'exemple marquées `À VÉRIFIER`.
- Un modèle absent du fichier est signalé, jamais valorisé à zéro en silence.
- Modèle gratuit (palier d'essai) : exiger un **tarif de référence** explicite et l'afficher comme « coût estimé ».
- Coût d'une variante = tokens moyens × prix, affiché pour 1 000 appels, et en euros par mois si `monthly_volume` est fourni.

## Fournisseurs

- Deux adaptateurs : `anthropic` et `openai_compatible` (URL de base configurable).
- Le catalogue gratuit de build.nvidia.com serait atteignable par le second. Selon des sources tierces non confirmées, l'URL de base est `https://integrate.api.nvidia.com/v1` et la limite est d'environ 40 requêtes par minute et par modèle, pour du prototypage seulement. **À vérifier dans la documentation officielle de NVIDIA ; ne rien coder en dur.**
- Limiteur de débit par modèle (`rpm`), nouvelle tentative avec attente croissante **uniquement** sur erreur 429 ou 5xx, limitée (4 essais) ; si elle échoue, la ligne est marquée en échec sans faire tomber le reste. Un 400, un 401, un 404, une erreur réseau ou un `usage` absent échouent aussitôt, avec un message clair sans texte du SDK.
- Un appel en échec est **exclu de la note** (il ne compte pas comme une mauvaise réponse) ; le rapport signale les variantes dont plus de 10 % des appels ont échoué.
- Après **3 refus de clé (401 ou 403) consécutifs**, le modèle concerné est arrêté (les autres continuent) ; le rapport le marque « non testé » avec la raison.
- Les réponses tronquées (`max_tokens` atteint) restent notées comme les autres, mais leur part par variante est comptée à part et le rapport conseille d'augmenter `max_tokens`.

## Sortie

- Tableau dans le terminal et `results.csv`, une ligne par variante et par modèle, triées par coût : variante, leviers, note, intervalle, tokens moyens en entrée et en sortie, coût pour 1 000 appels, statut (retenue, quasi à égalité, sous le seuil).
- `report.md` : verdict en une phrase, économie par rapport à `base_prompt`, résultat de la validation, tableau, date et source des prix.
- `best_prompt.txt` et `best_config.json` (modèle, paramètres, exemples inclus) : prêts à coller dans le workflow du client.

## Contraintes techniques

- Python 3.11 ou plus. Dépendances minimales : `anthropic`, `openai`, `pyyaml`, `rich`. Pas de framework d'orchestration.
- **Moteur séparé de la ligne de commande** : logique importable, CLI dans un fichier à part.
- Avant de lancer : afficher le nombre d'appels et un coût maximum estimé, demander confirmation, respecter `max_calls`.
- `--dry-run` avec faux modèle reproductible (option `--seed`) : tout le pipeline sans dépenser.
- Sorties brutes dans `runs/` pour pouvoir renoter sans réappeler l'API.
- Langue : messages, rapport et README en français ; code, commentaires et noms en anglais.

## Sécurité

Clé d'API uniquement par variable d'environnement ou `.env` local. Fournir `.env.example`. `.gitignore` : `.env`, `runs/`, résultats. Jamais de clé dans le code, les journaux ou les fichiers de sortie.

## Structure proposée

```
save-run/
  README.md              l'idée en dix lignes, installation, exemple de résultat
  LICENSE                MIT
  BRIEF.md
  config.example.yaml
  cases.example.csv
  prices.yaml
  .env.example
  .gitignore
  prototypes/            fichiers fournis, à ne pas modifier
  src/saverun/
    splitting.py         séparation exemples, sélection, validation
    variants.py
    runner.py            adaptateurs, limiteur de débit, nouvelles tentatives
    scoring.py           exact, json_fields, Wilson
    cost.py
    selection.py         seuil, égalités, validation
    report.py            CSV, Markdown, export
    diagnose.py
    cli.py
  tests/
```

## Tests attendus

Tests unitaires sur Wilson (valeurs vérifiables à la main), la notation, le coût, la séparation des cas (aucun chevauchement) et la sélection (seuil sur la borne basse, aucune variante retenue, égalités, validation sur note brute sans bascule vers un autre candidat). Test de bout en bout avec `--dry-run`. Test du diagnostic sur `calls.example.csv`.

## Façon de travailler

1. Plan court d'abord, à me montrer avant d'écrire du code.
2. Un commit par étape.
3. Me demander avant toute décision qui change le périmètre.
4. Si une consigne te semble mauvaise ou contradictoire, le dire plutôt que de la contourner.
5. Aucun appel payant et aucun push sans mon accord.

## Terminé quand

Avec un `config.yaml` et un `cases.csv` réels, `saverun run` produit le tableau, `report.md`, `best_prompt.txt` et `best_config.json`, avec une colonne de coût cohérente avec les tokens renvoyés par l'API et une validation sur cas mis de côté. `saverun diagnose` classe la dépense par tâche. `--dry-run` et les tests passent.

## Décisions de construction (ajoutées pendant la v0)

Choix faits avec l'auteur en cours de route ; ils complètent ou précisent le brief.

- **Configuration** : champs ajoutés `role` (active la persona « métier »), `fields` (champs comparés en `json_fields`), `seed` (séparation des cas et tirage des variantes), `max_tokens` (300 par défaut, à augmenter pour de l'extraction JSON). `temperature` par modèle est optionnelle : absente, elle n'est jamais envoyée à l'API et le rapport indique « température : défaut du fournisseur » ; l'avertissement sur `runs_per_case` ne vaut que pour une température explicitement à 0.
- **Notation** : `json_fields` est tout-ou-rien (nécessaire pour Wilson) ; le détail par champ est indicatif dans le rapport. Les doublons d'`input` dans `cases.csv` sont retirés avec un avertissement.
- **Prix** : `prices.yaml` porte une `currency` (aucune conversion, le rapport affiche cette monnaie), et une `date` et une `source` par modèle. Un modèle absent bloque le lancement.
- **Variantes** : la combinaison la plus sobre est toujours `V01` ; les autres sont tirées avec la graine. Le `base_prompt` est exécuté comme variante de référence `BASE`.
- **Réutilisation de `runs/`** : un appel déjà réussi n'est pas repayé si modèle, prompt, cas (id et texte), passage, température et `max_tokens` sont identiques.
- **Économie** : le champ optionnel `current_model` désigne le modèle qui exécute aujourd'hui le prompt actuel (il doit figurer dans `models`, sinon erreur). L'économie se calcule entre `BASE` sur ce modèle et le gagnant. Sans `base_prompt` ou sans `current_model`, aucune hypothèse n'est faite : le rapport affiche les coûts, sans chiffre d'économie, et dit pourquoi. Si `BASE` ne démontre pas lui-même le seuil, le rapport affiche sa note et précise que l'économie n'est pas « à qualité égale ».
- **Variante instable** : une variante dont plus de 10 % des appels ont échoué reste éligible ; si elle gagne, le rapport la marque « non comparable équitablement » et « à relancer ».
- **Simulation (`--dry-run`)** : bandeau « SIMULATION : faux modèle, résultats sans valeur » dans le terminal et dans `report.md`, colonne `simulation` dans `results.csv`, `best_prompt.txt` et `best_config.json` non écrits, réponses simulées rangées à part (`runs/simulation/`) pour ne jamais être réutilisées par un vrai run.
- **Rapport** : il indique la température de chaque modèle (« défaut du fournisseur » si absente), la part de réponses tronquées, les modèles non testés, la monnaie, la date et la source des prix, et avertit si des prix sont « À VÉRIFIER ». `report.md` se termine par « Généré avec SAVE Run (https://allrun.be) », retirable avec `--no-footer`.
- **Dossier de sortie** : `saverun-out/` par défaut (`saverun-out/dry-run/` en simulation), modifiable avec `--out`.
- **Format de réponse** : le champ optionnel `output_format` est ajouté à chaque variante générée, y compris sans exemple (il remplace la ligne de format générique ; avec raisonnement, il s'applique à la réponse finale, après « Réponse : »). Avec `scoring: json_fields` et sans `output_format`, un avertissement clair s'affiche sans bloquer : les variantes ne sauraient pas quel JSON produire.
- **Tirage des variantes équilibré** : V01 reste la plus sobre, et au moins la moitié des variantes (V01 comprise) sont sans raisonnement, car le raisonnement coûte des tokens en sortie et gagne rarement sur le prix. Les identifiants restent stables pour une configuration, un fichier de cas, une graine et un `n_variants` donnés.
- **Raisonnement et `output_format`** : le format est placé avant la consigne de raisonnement, qui précise qu'il ne s'applique qu'à ce qui suit « Réponse : » (le raisonnement reste libre, rien ne suit la réponse), ce qui évite toute contradiction avec « sans texte avant ou après ». Sans `output_format`, la formulation d'origine est conservée.
- **Exemples few-shot** : au moins un exemple par valeur attendue distincte quand les 3 emplacements le permettent ; sinon un avertissement nomme les valeurs absentes. La valeur attendue est celle que la notation compare (avec `json_fields`, seulement les champs de `fields`). L'ordre des exemples est mélangé pour ne pas suivre les valeurs.
- **Colonne `tag` optionnelle** : `cases.csv` peut ajouter une colonne `tag` pour afficher une « Note par tag » sur les cas de sélection. Elle est purement informative : le tag n'est jamais envoyé au modèle et n'entre ni dans la clé de réutilisation, ni dans la séparation des cas, ni dans la sélection. Les fichiers à deux colonnes restent valides. Moins de 10 cas de sélection sous un tag déclenche un avertissement.
