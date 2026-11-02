#!/usr/bin/env python3
"""Idée de script : génère des variantes d'un prompt, les teste, compare note et coût.

Usage :
  python saverun.py cases.csv --dry-run                  # faux modèle, aucun appel, aucun coût
  python saverun.py cases.csv --model <id-du-modèle>     # vrai modèle (clé ANTHROPIC_API_KEY requise)
"""
import argparse
import csv
import itertools
import math
import os
import random
import sys
import zlib

# € par million de tokens. VALEURS D'EXEMPLE À REMPLACER PAR LES TARIFS DU MOMENT.
PRICES = {"in": 1.00, "out": 5.00}

INTENTION = ("Classe le ticket de support client dans une seule catégorie parmi : "
             "facturation, technique, livraison, autre.")
ROLE = "un agent de support client expérimenté"


# --- 1. Génération des variantes : combinaisons de leviers -------------------

def build_variants(examples, n, seed=1):
    """Chaque variante = une combinaison de leviers. Le tirage est fixé (seed) pour être reproductible."""
    combos = list(itertools.product(
        ["aucun", "générique", "métier"],   # persona (« aucun » = témoin)
        [0, 3],                             # nombre d'exemples
        [False, True],                      # raisonnement étape par étape
        [False, True],                      # consigne condensée
    ))
    random.Random(seed).shuffle(combos)
    variants = []
    for i, (persona, k, reasoning, concise) in enumerate(combos[:n], start=1):
        parts = []
        if persona == "générique":
            parts.append("Tu es un assistant précis.")
        elif persona == "métier":
            parts.append(f"Tu es {ROLE}.")
        parts.append(INTENTION if concise else
                     INTENTION + " Lis attentivement la demande et applique la consigne telle quelle.")
        if k:
            shots = "\n".join(f"Ticket : {e['input']}\nCatégorie : {e['expected']}" for e in examples[:k])
            parts.append("Exemples :\n" + shots)
        parts.append("Réfléchis étape par étape, puis écris la réponse finale après « Réponse : »."
                     if reasoning else "Réponds uniquement par le nom de la catégorie.")
        variants.append({
            "id": f"V{i:02d}",
            "leviers": f"persona={persona}, exemples={k}, raisonnement={'oui' if reasoning else 'non'}, "
                       f"condensé={'oui' if concise else 'non'}",
            "prompt": "\n\n".join(parts),
            "reasoning": reasoning,
        })
    return variants


# --- 2. Appel du modèle ------------------------------------------------------

def call_model(prompt, user_input, model, dry_run, expected=None, seed=0):
    """Renvoie (texte, tokens_entrée, tokens_sortie)."""
    if dry_run:  # faux modèle : meilleure chance de réussir avec exemples / raisonnement
        rnd = random.Random(zlib.crc32((prompt + user_input).encode()) + seed)  # reproductible
        p = 0.55 + 0.15 * ("Exemples" in prompt) + 0.10 * ("étape par étape" in prompt) \
            + 0.05 * ("Tu es" in prompt)
        answer = expected if rnd.random() < p else "autre"
        text = ("Je réfléchis... Réponse : " + answer) if "étape par étape" in prompt else answer
        return text, (len(prompt) + len(user_input)) // 4, len(text) // 4
    import anthropic  # chargé seulement si on appelle un vrai modèle
    client = anthropic.Anthropic()
    r = client.messages.create(model=model, max_tokens=300, system=prompt,
                               messages=[{"role": "user", "content": user_input}])
    return r.content[0].text, r.usage.input_tokens, r.usage.output_tokens


# --- 3. Notation et statistiques ---------------------------------------------

def is_correct(output, expected, reasoning):
    if reasoning and "Réponse :" in output:
        output = output.split("Réponse :")[-1]
    return output.strip().lower().strip(".") == expected.strip().lower()


def wilson(k, n, z=1.96):
    """Intervalle de confiance à 95 % sur une proportion (méthode de Wilson)."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    marge = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return centre - marge, centre + marge


def cost_per_1000(avg_in, avg_out):
    return (avg_in * PRICES["in"] + avg_out * PRICES["out"]) * 1000 / 1_000_000


# --- 4. Programme principal --------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cases")
    ap.add_argument("--model", default="")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--seuil", type=float, default=0.75,
                    help="qualité minimale exigée (0 à 1) ; la borne basse de l'intervalle doit la dépasser")
    ap.add_argument("--runs", type=int, default=1,
                    help="passages par cas : la note d'un cas est la moyenne de ses passages")
    ap.add_argument("--seed", type=int, default=0, help="--dry-run seulement : change le hasard du faux modèle")
    ap.add_argument("--out", default="results.csv")
    a = ap.parse_args()

    with open(a.cases, newline="", encoding="utf-8") as f:
        cases = list(csv.DictReader(f))
    examples, test_cases = cases[:3], cases[3:]  # les exemples ne servent jamais à la notation
    if len(test_cases) < 20:
        print(f"⚠ {len(test_cases)} cas de test seulement : résultats indicatifs (visez 20 à 50).")

    variants = build_variants(examples, a.n)
    n_calls = len(variants) * len(test_cases) * a.runs
    print(f"{len(variants)} variantes × {len(test_cases)} cas × {a.runs} passage(s) = {n_calls} appels.")
    if not a.dry_run and not a.yes and input("Lancer ? [o/N] ").lower() != "o":
        sys.exit("Annulé.")
    if not a.dry_run and not a.model:
        sys.exit("Indiquez --model <id-du-modèle>.")

    rows = []
    for v in variants:
        ok, tin, tout = 0.0, 0, 0
        for c in test_cases:
            wins = 0
            for r in range(a.runs):
                text, i, o = call_model(v["prompt"], c["input"], a.model, a.dry_run,
                                        c["expected"], a.seed + 1000 * r)
                wins += is_correct(text, c["expected"], v["reasoning"])
                tin += i
                tout += o
            ok += wins / a.runs  # taux de réussite du cas, entre 0 et 1
        n = len(test_cases)       # l'unité statistique reste le cas : l'intervalle ne se resserre qu'avec plus de cas
        calls = n * a.runs
        lo, hi = wilson(ok, n)
        rows.append({"variante": v["id"], "leviers": v["leviers"], "note": ok / n,
                     "ic95_bas": lo, "ic95_haut": hi, "tokens_entrée": tin / calls,
                     "tokens_sortie": tout / calls,
                     "coût_1000_appels_eur": cost_per_1000(tin / calls, tout / calls)})

    rows.sort(key=lambda r: r["coût_1000_appels_eur"])
    # Retenue : la moins chère dont la borne basse de l'intervalle dépasse le seuil.
    star = next((r["variante"] for r in rows if r["ic95_bas"] >= a.seuil), None)
    # Quasi à égalité : autres variantes qui démontrent aussi le seuil, à moins de 10 % du coût de la ★.
    ties = []
    if star:
        star_cost = next(r["coût_1000_appels_eur"] for r in rows if r["variante"] == star)
        ties = [r["variante"] for r in rows if r["variante"] != star and r["ic95_bas"] >= a.seuil
                and r["coût_1000_appels_eur"] <= star_cost * 1.10]

    print(f"\n{'Var.':<5}{'Note':>6}{'IC 95 %':>14}{'Tok.in':>8}{'Tok.out':>9}{'€/1000':>9}  Leviers")
    for r in rows:
        mark = " ★" if r["variante"] == star else ""
        ic = "{:.0%}-{:.0%}".format(r["ic95_bas"], r["ic95_haut"])
        print(f"{r['variante']:<5}{r['note']:>6.0%}{ic:>14}"
              f"{r['tokens_entrée']:>8.0f}{r['tokens_sortie']:>9.0f}{r['coût_1000_appels_eur']:>9.3f}  "
              f"{r['leviers']}{mark}")
    if star:
        print(f"★ = la moins chère dont la borne basse de l'intervalle dépasse {a.seuil:.0%}.")
        if ties:
            print(f"Quasi à égalité avec {star} (même seuil démontré, coût à ±10 %) : {', '.join(ties)}. "
                  "Le choix entre elles ne repose pas sur une différence démontrée.")
    else:
        print(f"Aucune variante ne démontre {a.seuil:.0%} de qualité : baissez le seuil ou ajoutez des cas de test.")

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"Résultats écrits dans {a.out}")


if __name__ == "__main__":
    main()
