#!/usr/bin/env python3
"""Diagnostic de facture : où part l'argent ? (étape 0 de SAVE Run)

Lit un export d'appels d'API et sort la dépense par tâche, triée, avec la part cumulée
et une projection mensuelle. Sert à choisir les 1 à 3 tâches qui valent la peine d'être optimisées.

Usage :
  python diagnostic.py calls.csv --prix prix.csv

calls.csv : une ligne par appel, colonnes  date, tache, modele, tokens_entree, tokens_sortie
            (date au format AAAA-MM-JJ ; "tache" = nom du workflow ou de la fonction qui appelle le modèle)
prix.csv  : colonnes  modele, eur_par_million_entree, eur_par_million_sortie
            VALEURS À RENSEIGNER avec les tarifs du moment : ce script n'en invente aucun.
"""
import argparse
import csv
import sys
from collections import defaultdict
from datetime import date


def lire_prix(chemin):
    prix = {}
    with open(chemin, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            prix[r["modele"].strip()] = (float(r["eur_par_million_entree"]),
                                         float(r["eur_par_million_sortie"]))
    return prix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("appels")
    ap.add_argument("--prix", required=True)
    ap.add_argument("--part", type=float, default=0.80,
                    help="part de la dépense à couvrir pour choisir les tâches à optimiser (défaut 80 %%)")
    ap.add_argument("--seuil-interet", type=float, default=100.0,
                    help="dépense mensuelle (€) en dessous de laquelle l'optimisation ne vaut sans doute pas "
                         "le temps passé. Valeur arbitraire : à fixer selon ton offre.")
    ap.add_argument("--out", default="diagnostic.csv")
    a = ap.parse_args()

    prix = lire_prix(a.prix)
    taches = defaultdict(lambda: {"appels": 0, "tin": 0, "tout": 0, "cout": 0.0, "modeles": defaultdict(int)})
    jours, ignorees, sans_prix = [], 0, defaultdict(int)

    with open(a.appels, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                d = date.fromisoformat(r["date"].strip()[:10])
                tin, tout = int(r["tokens_entree"]), int(r["tokens_sortie"])
                tache, modele = r["tache"].strip(), r["modele"].strip()
            except (KeyError, ValueError, AttributeError):
                ignorees += 1
                continue
            jours.append(d)
            t = taches[tache]
            t["appels"] += 1
            t["tin"] += tin
            t["tout"] += tout
            t["modeles"][modele] += 1
            if modele in prix:
                t["cout"] += (tin * prix[modele][0] + tout * prix[modele][1]) / 1_000_000
            else:
                sans_prix[modele] += 1  # compté à part : jamais valorisé à zéro en silence

    if not jours:
        sys.exit("Aucun appel exploitable dans le fichier.")

    etendue = (max(jours) - min(jours)).days + 1
    facteur = 30 / etendue
    total = sum(t["cout"] for t in taches.values())
    mensuel = total * facteur

    print(f"{sum(t['appels'] for t in taches.values())} appels sur {etendue} jour(s), "
          f"du {min(jours)} au {max(jours)}.")
    if etendue < 7:
        print("⚠ moins de 7 jours de données : la projection mensuelle est fragile.")
    if ignorees:
        print(f"⚠ {ignorees} ligne(s) illisible(s) ignorée(s).")
    if sans_prix:
        manquants = ", ".join(f"{m} ({n} appels)" for m, n in sans_prix.items())
        print(f"⚠ modèles absents de {a.prix}, non valorisés : {manquants}. Le total est sous-estimé.")

    lignes = sorted(taches.items(), key=lambda kv: kv[1]["cout"], reverse=True)
    print(f"\n{'Tâche':<26}{'Appels/mois':>12}{'€/mois':>10}{'Part':>7}{'Cumul':>7}  Modèle principal")
    cumul, retenues, rows = 0.0, [], []
    for nom, t in lignes:
        part = t["cout"] / total if total else 0
        avant = cumul
        cumul += part
        mois = t["cout"] * facteur
        principal = max(t["modeles"], key=t["modeles"].get)
        marque = ""
        if avant < a.part and total:  # on retient les tâches jusqu'à couvrir la part visée
            retenues.append(nom)
            marque = " ◄"
        print(f"{nom:<26}{t['appels'] * facteur:>12.0f}{mois:>10.2f}{part:>7.0%}{cumul:>7.0%}  {principal}{marque}")
        rows.append({"tache": nom, "appels_mois": round(t["appels"] * facteur),
                     "eur_mois": round(mois, 2), "part": round(part, 4), "cumul": round(cumul, 4),
                     "modele_principal": principal, "a_optimiser": bool(marque)})

    print(f"\nTotal valorisé : {mensuel:.2f} €/mois ({mensuel * 12:.0f} €/an) au rythme observé.")
    if mensuel < a.seuil_interet:
        print(f"→ Sous {a.seuil_interet:.0f} €/mois : l'optimisation ne se justifie sans doute pas.")
    else:
        print(f"→ À optimiser en priorité ({a.part:.0%} de la dépense) : {', '.join(retenues)}")
    print("◄ = tâche retenue pour un test SAVE Run.")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Détail écrit dans {a.out}")


if __name__ == "__main__":
    main()
