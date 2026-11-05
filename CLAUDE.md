# SAVE Run

CLI qui, pour une automatisation à prompt fixe, génère des variantes de prompt, les exécute sur plusieurs modèles, les note de façon déterministe et recommande le couple (prompt, modèle) le moins cher qui démontre un seuil de qualité. `BRIEF.md` fait foi pour la spécification et ses décisions de construction ; `README.md` s'adresse aux utilisateurs.

## Règles permanentes

- **Aucune suppression sans demander**, ni fichier ni dossier, et jamais de `rm -rf` dans le projet. Les essais se font dans un dossier temporaire hors du projet.
- **Ne jamais lire ni afficher `.env`.** Pour savoir si une clé est là, répondre seulement « présente » ou « absente ». Aucune clé dans le code, les journaux ou les fichiers de sortie.
- **Aucun push.**
- **Aucun appel payant sans accord explicite. L'utilisateur lance lui-même les runs payants** : donner la commande, ne pas l'exécuter. Un accord vaut pour ce qu'il nomme, pas pour la suite.
- **Ne rien inventer** : aucun prix sans source ni date. Si une information manque, s'arrêter et demander.
- Plan court d'abord quand l'utilisateur le demande, puis un commit par sujet.

Règles non négociables du produit :
1. La sélection se fait sur la **borne basse** de l'intervalle de Wilson à 95 %, jamais sur la meilleure note brute.
2. Un modèle absent du fichier de prix, ou marqué « À VÉRIFIER », bloque le lancement, même en `--dry-run`.
3. Pas de clé d'API ailleurs que dans l'environnement ou un `.env` local.

## État du projet

- v0 fonctionnelle : `saverun run`, `saverun diagnose`, `--dry-run` avec faux modèle.
- Testée sur trois runs réels, sur une seule tâche de classification (détection d'urgence sur e-mails synthétiques) et trois modèles : deux chez Anthropic, un gratuit chez NVIDIA via `openai_compatible`.
- Dépôt privé pour l'instant, sans remote, mais écrit comme s'il allait devenir public.
- Résultat principal : sur ce jeu de test, plusieurs variantes atteignent presque la même note. Le choix repose donc surtout sur le coût, et le rapport le signale.

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
- La clé de réutilisation contient modèle, hash du prompt, cas, hash de l'entrée, passage, température et `max_tokens`. Changer l'un d'eux refait, et repaie, les appels.

## Conventions

- Code, docstrings et commentaires en anglais ; textes affichés, rapports, README et messages de commit en français.
- Tests avec de faux clients, jamais d'appel réel. Ils passent `--env` vers un fichier inexistant de `tmp_path`.
- Le README contient un exemple généré par le faux modèle, vérifié par un test : à régénérer s'il dérive.
- Utilitaires hors paquet dans `tools/`. `.gitattributes` force les fins de ligne LF.
- Données réelles dans `private/` ou `data/`, sorties dans `saverun-out/` : ces dossiers, `.env`, `config.yaml`, `cases.csv` et `NOTES.local.md` sont ignorés par git.

## Décisions et raisons

- **`max_tokens` reste à 500.** Des variantes à raisonnement sont tronquées, ce qui explique leurs erreurs sur Haiku. Mais ces variantes ne gagneraient pas de toute façon, et tout refaire coûterait environ 3 USD pour rien. Les réponses tronquées restent notées comme les autres.
- **Modèle gratuit : tarif de référence déclaré.** Il est affiché comme « coût estimé », pas comme une économie réelle. Faute de tarif payant pour `nemotron-3.5-bolt-e4b`, on utilise celui d'un autre modèle (Super VL), en le disant dans le champ `source`. Ce choix peut décider du gagnant : on a pris un tarif prudent, qui ne favorise pas le modèle.
- **Le seuil est une décision métier.** Le même test peut donner un autre gagnant à 70 % et à 95 %. Avec n cas de sélection, la borne basse maximale est n/(n+3,84) : 95 % demande au moins 73 cas de sélection, soit environ 100 au total.
- **Validation :** le gagnant est confirmé si sa note brute atteint le seuil, car un petit ensemble ne permet pas à la borne basse de l'atteindre. « Indicatif » sous 20 cas, et jamais de changement de candidat.
- **Avertissement « le jeu de test ne discrimine pas »**, par modèle. Règle A : au moins 80 % des variantes à 98 % ou plus. Règle B : sans raisonnement, un écart d'au plus 3 points ou 1 cas, et la meilleure au seuil.
- **Colonne `tag` purement informative :** ni dans le prompt, ni dans la clé de réutilisation, ni dans la séparation des cas, ni dans la sélection.

## Reste à faire

- Avant toute publication, et seulement à la demande de l'utilisateur : le nettoyage de l'historique local décrit dans `NOTES.local.md`.
- Remplacer `<url-du-dépôt>` dans le README au moment de publier.
- Vérifier la limite de débit réelle de NVIDIA dans sa console : aucun chiffre officiel trouvé, `rpm` est à 30.
- Corriger l'ancienne URL `allrun.ai` à la main dans les anciens rapports, s'ils sont partagés.
- Relancer un run sur le fichier avec tags pour voir la section « Note par tag » sur de vraies données.
- Revoir les critères ou l'étiquette de l'e-mail « Rendez-vous demain » : les critères d'urgence ne citent pas le lendemain, alors que l'étiquette attend `true`.
