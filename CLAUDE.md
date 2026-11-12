# SAVE Run

CLI qui, pour une automatisation à prompt fixe, génère des variantes de prompt, les exécute sur plusieurs modèles, les note de façon déterministe et recommande le couple (prompt, modèle) le moins cher qui démontre un seuil de qualité. `BRIEF.md` fait foi pour la spécification et ses décisions de construction ; `README.md` s'adresse aux utilisateurs.

## Règles permanentes

- **Aucune suppression sans demander**, ni fichier ni dossier, et jamais de `rm -rf` dans le projet. Les essais se font dans un dossier temporaire hors du projet.
- **Ne jamais lire ni afficher `.env`.** Pour savoir si une clé est là, répondre seulement « présente » ou « absente ». Aucune clé dans le code, les journaux ou les fichiers de sortie.
- **Aucun push.**
- **Aucun appel payant sans accord explicite. L'utilisateur lance lui-même les runs payants** : donner la commande, ne pas l'exécuter. Un accord vaut pour ce qu'il nomme, pas pour la suite.
- **Ne rien inventer** : aucun prix sans source ni date. Si une information manque, s'arrêter et demander.
- Plan court d'abord quand l'utilisateur le demande, puis un commit par sujet.
- **Demander avant toute décision qui change le périmètre.**

Règles non négociables du produit :
1. La sélection se fait sur la **borne basse** de l'intervalle de Wilson à 95 %, jamais sur la meilleure note brute.
2. Un modèle absent du fichier de prix, ou dont les deux tarifs valent encore « À VÉRIFIER », bloque le lancement, même en `--dry-run`. Si les tarifs sont des nombres et que seuls `date` ou `source` sont « À VÉRIFIER », le lancement continue avec un avertissement : les coûts ne sont pas fiables.
3. Pas de clé d'API ailleurs que dans l'environnement ou un `.env` local.

## État du projet

- v0 fonctionnelle : `saverun run`, `saverun diagnose`, `--dry-run` avec faux modèle.
- Testée sur trois runs réels, sur une seule tâche de classification (détection d'urgence sur e-mails synthétiques) et trois modèles : deux chez Anthropic, un gratuit chez NVIDIA via `openai_compatible`.
- Dépôt privé pour l'instant, sans remote, mais écrit comme s'il allait devenir public.
- Résultat principal : sur ce jeu de test, plusieurs variantes atteignent presque la même note. Le choix repose donc surtout sur le coût, et le rapport le signale.
- Quatrième run réel, sur un jeu public : Banking77, 12 intentions proches, 204 cas, seuil 90 % (`saverun-out/banking77`). Aucune variante ne démontre le seuil. 27 des 151 cas de sélection sont ratés par au moins 20 des 27 couples, souvent avec une étiquette qui semble contredire le message (non corrigée).
- Depuis ce run : lecture tolérante du marqueur de réponse, et décompte des réponses « hors format » par variante dans le rapport.
- Banking77 selon le seuil, avec la lecture tolérante : à 0,90 aucune variante n'est retenue ; à 0,70, V01 sur Haiku, confirmé en validation (38/50) ; à 0,65, BASE sur Haiku, confirmé en validation (38/50). Validation des deux seuils : 150 appels Haiku, coût réel 0,011 USD, sélection entièrement reprise du cache.
- Dossiers : `saverun-out/banking77` (rapport renoté, seuil 0,90), `banking77-avant-correction` (rapport d'origine), `banking77-seuil70` et `banking77-seuil65`.
- À dire avec ces résultats : les seuils 0,70 et 0,65 ont été choisis après avoir vu les résultats ; le BASE est un prompt naïf écrit par nous ; « confirmé » veut dire « note brute de validation ≥ seuil », pas « seuil démontré » : sur 50 cas, l'intervalle est 63–86 %, borne basse sous 70 %, et la validation ne démontre pas à elle seule le seuil.

## Commandes

```bash
pip install -e .
python -m pytest -q                # tests, sans réseau ni clé, sans charger le vrai .env

# Contrôle sans dépense (faux modèle)
saverun run --config config.example.yaml --cases cases.example.csv --dry-run

# Run réel : c'est l'utilisateur qui le lance
python -m saverun.cli run --config config.yaml --cases private/<cas>.csv --prices private/<prix>.yaml --out saverun-out/<nom>

# Outils hors paquet
python tools/make_cases.py private/<emails>.csv -o private/<cas>.csv   # 2 ou 3 colonnes : texte, oui/non, tag
python tools/per_family.py saverun-out/<nom> private/<familles>.csv    # note par famille à partir des appels déjà faits
```

Pièges connus :
- `--dry-run --out <dossier>` réécrit `report.md` et `results.csv` de ce dossier, et lit un cache séparé (`runs/simulation/`). Il n'est donc ni sûr dans le dossier d'un vrai run, ni un test du vrai cache. Pour vérifier le cache : `prepare()` avec le vrai `RunStore`, sans écriture ni appel.
- Un vrai run avec un `--out` existant remplace `report.md`, `results.csv` et `best_*`.
- La clé de réutilisation contient modèle, hash du prompt, cas, hash de l'entrée, passage, température et `max_tokens`. Changer l'un d'eux refait, et repaie, les appels. `ANSWER_MARKER` est écrit dans les prompts : ne pas le modifier (un test le vérifie).
- `results.csv` a une colonne `off_format_share` (après `truncated_share`), vide quand elle n'est pas calculable. Lire les colonnes de `results.csv` par leur nom, jamais par leur position.

## Conventions

- Code, docstrings et commentaires en anglais ; textes affichés, rapports, README et messages de commit en français.
- Tests avec de faux clients, jamais d'appel réel. Ils passent `--env` vers un fichier inexistant de `tmp_path`.
- Le README contient un exemple généré par le faux modèle, vérifié par un test : à régénérer s'il dérive.
- Utilitaires hors paquet dans `tools/`. `.gitattributes` force les fins de ligne LF.
- Données réelles dans `private/` ou `data/`, sorties dans `saverun-out/` : ces dossiers, `.env`, `config.yaml`, `cases.csv` et les notes locales sont ignorés par git.

## Décisions et raisons

- **`max_tokens` reste à 500 dans le `config.yaml` local** (le défaut du code est 300). Des variantes à raisonnement sont tronquées, ce qui explique leurs erreurs sur Haiku. Mais ces variantes ne gagneraient pas de toute façon, et tout refaire coûterait environ 3 USD pour rien. Les réponses tronquées restent notées comme les autres.
- **Modèle gratuit : tarif de référence déclaré.** Il est affiché comme « coût estimé », pas comme une économie réelle. Faute de tarif payant pour `nemotron-3.5-bolt-e4b`, on utilise celui d'un autre modèle (Super VL), en le disant dans le champ `source`. Ce choix peut décider du gagnant : on a pris un tarif prudent, qui ne favorise pas le modèle.
- **Le seuil est une décision métier.** Le même test peut donner un autre gagnant à 70 % et à 95 %. Avec n cas de sélection, la borne basse maximale est n/(n+3,84) : 95 % demande au moins 73 cas de sélection, soit environ 100 au total.
- **Validation :** le gagnant est confirmé si sa note brute atteint le seuil, car un petit ensemble ne permet pas à la borne basse de l'atteindre. « Indicatif » sous 20 cas, et jamais de changement de candidat.
- **Avertissement « le jeu de test ne discrimine pas »**, par modèle. Règle A : au moins 80 % des variantes à 98 % ou plus. Règle B : sans raisonnement, un écart d'au plus 3 points ou 1 cas, et la meilleure au seuil.
- **Colonne `tag` purement informative :** ni dans le prompt, ni dans la clé de réutilisation, ni dans la séparation des cas, ni dans la sélection.
- **Test Banking77 : le BASE est écrit par nous**, volontairement naïf et sans définitions (il liste seulement les 12 étiquettes). Le dire dans toute publication : l'économie et l'écart de qualité sont mesurés contre ce prompt, pas contre un prompt réel d'entreprise. Les définitions des 12 étiquettes, dans l'intention, sont aussi écrites par nous.
- **Test Banking77 : 3 messages exclus du tirage**, lus pendant la rédaction du prompt (en-tête et premières lignes de `test.csv`), tous `card_arrival` : « How do I locate my card? », « I still have not received my new card, I ordered over a week ago. », « I ordered a card but it has not arrived. Help please! ». Le tirage se fait dans une copie filtrée de `test.csv` dans `private/banking77/`. La définition de `card_arrival` a été réécrite à partir du seul nom de l'étiquette. Source des messages : Banking77, Casanueva, Temčinas, Gerz, Henderson et Vulić, « Efficient Intent Detection with Dual Sentence Encoders », NLP for ConvAI, ACL 2020, licence CC BY 4.0.
- **Banking77 est public et probablement présent dans les données d'entraînement des modèles** testés (et de l'assistant qui a rédigé les définitions). À mentionner dans toute publication.
- **Réponses « hors format »** : réponses abouties, non tronquées, illisibles une fois extraites (en `json_fields`, pas d'objet JSON avec les champs notés ; en `exact`, une valeur hors de la liste fermée des valeurs attendues, après la même normalisation que la notation, et seulement si cette liste a au plus deux fois moins de valeurs que de cas). Informatif : la note ne change pas.
- **Toute correction de notation appliquée après un run garde l'ancien dossier de résultats.** Copier d'abord le dossier (par exemple `saverun-out/<nom>-avant-correction`), sans rien supprimer, puis renoter depuis le cache sans aucun appel. Le rapport renoté dit qu'il a été renoté, avec quelle correction.
- **Définitions gelées dès le lancement du run.** Aucune modification de l'intention, des définitions ou du BASE après avoir vu des résultats. Si une modification s'impose, la noter (quoi, pourquoi, après quel résultat) et relancer un test neuf, dans un nouveau dossier de sortie.

## Reste à faire

- Vérifier la limite de débit réelle de NVIDIA dans sa console : aucun chiffre officiel trouvé, `rpm` est à 30.
- Revoir les critères ou l'étiquette de l'e-mail « Rendez-vous demain » : les critères d'urgence ne citent pas le lendemain, alors que l'étiquette attend `true`.

## Idées v1, ne pas coder sans accord

- Signaler les cas ratés par presque toutes les variantes d'un modèle.
- Mode sans étiquettes, avec les réponses du modèle actuel comme référence.
- Pondération des erreurs (rater une urgence ou sur-détecter).
- Noter toute modification d'étiquette décidée après lecture des résultats.
- Formules ajoutées par les variantes (persona, « Lis attentivement… », « Exemples : / Entrée : / Sortie : », consigne de raisonnement, « Réponse : ») dans la langue de la configuration, au lieu du français fixe.
- `saverun rescore` : renoter un run depuis son cache, sans aucun appel. La commande refuse de continuer s'il manque un appel, y compris de validation, n'a pas besoin de clé et ne charge pas `.env`. Elle écrit dans un nouveau dossier, ou garde une copie de l'ancien, et le rapport dit qu'il a été renoté.
