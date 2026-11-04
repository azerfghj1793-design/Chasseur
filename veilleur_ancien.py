"""VEILLEUR — surveille Dealabs et alerte sur téléphone, sans IA ni clé API.

Usage :
  python veilleur.py          vérifie les annonces et envoie les alertes
  python veilleur.py --test   affiche ce qui correspond, sans alerter ni mémoriser

Les produits et les prix maximum se règlent dans surveillances.json.
"""

import argparse
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

DOSSIER = Path(__file__).parent
FICHIER_CONFIG = DOSSIER / "surveillances.json"
FICHIER_VUS = DOSSIER / "deals_vus.json"
FICHIER_PRIX = DOSSIER / "meilleurs_prix.json"

FLUX = ["https://www.dealabs.com/rss/new", "https://www.dealabs.com/rss/hot"]
PEPPER = "{http://www.pepper.com/rss}merchant"
NAVIGATEUR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def lire_prix(texte: str | None) -> float | None:
    """'1 090,23€' → 1090.23"""
    if not texte:
        return None
    nombre = re.sub(r"[^\d,.]", "", texte).replace(".", "").replace(",", ".")
    try:
        return float(nombre)
    except ValueError:
        return None


def lire_flux(url: str) -> list[dict]:
    requete = urllib.request.Request(url, headers=NAVIGATEUR)
    with urllib.request.urlopen(requete, timeout=30) as reponse:
        racine = ET.fromstring(reponse.read())

    annonces = []
    for item in racine.iter("item"):
        vendeur = item.find(PEPPER)
        annonces.append({
            "titre": item.findtext("title", "").strip(),
            "lien": item.findtext("link", "").strip(),
            "categorie": item.findtext("category", "").strip(),
            "vendeur": vendeur.get("name", "?") if vendeur is not None else "?",
            "prix": lire_prix(vendeur.get("price")) if vendeur is not None else None,
        })
    return annonces


def contient(titre: str, mot: str) -> bool:
    """Cherche un mot entier : 'tv' ne doit pas matcher 'activation'.

    Un mot finissant par * est un début de mot : 'oled55*' matche 'OLED55C5'.
    """
    fin = "" if mot.endswith("*") else r"(?!\w)"
    return re.search(rf"(?<!\w){re.escape(mot.rstrip('*').lower())}{fin}", titre) is not None


def trouver_modele(titre: str, regle: dict) -> str | None:
    """Renvoie le modèle suivi cité dans le titre, s'il y en a un."""
    # Le modèle peut être collé après des chiffres : 'C5' matche 'OLED55C5'.
    for modele in regle.get("modeles", []):
        if re.search(rf"(?<![a-z]){re.escape(modele.lower())}(?!\d)", titre.lower()):
            return modele
    return None


def correspond(annonce: dict, regle: dict) -> bool:
    titre = annonce["titre"].lower()
    if regle.get("modeles") and not trouver_modele(titre, regle):
        return False
    if not all(contient(titre, m) for m in regle.get("tous", [])):
        return False
    # Chaque groupe est une alternative : il faut au moins un mot par groupe.
    if not all(any(contient(titre, m) for m in groupe) for groupe in regle.get("groupes", [])):
        return False
    if any(contient(titre, m) for m in regle.get("exclure", [])):
        return False
    # Sans prix lisible, on garde l'annonce : mieux vaut une alerte de trop.
    return annonce["prix"] is None or annonce["prix"] <= regle["prix_max"]


def alerter(sujet: str, regle: dict, annonce: dict):
    prix = f"{annonce['prix']:.2f} €" if annonce["prix"] is not None else "prix non indiqué"
    if annonce.get("ancien_prix"):
        prix += f" (baisse : {annonce['ancien_prix']:.2f} € avant)"

    # Une surveillance avec un webhook part sur Discord, les autres sur ntfy.
    if regle.get("webhook_discord"):
        message = f"**{regle['nom']}**\n{annonce['titre']}\n{prix} chez {annonce['vendeur']}\n{annonce['lien']}"
        requete = urllib.request.Request(
            regle["webhook_discord"],
            data=json.dumps({"content": message}).encode("utf-8"),
            headers={**NAVIGATEUR, "Content-Type": "application/json"},
        )
        urllib.request.urlopen(requete, timeout=30).close()
        return

    requete = urllib.request.Request(
        f"https://ntfy.sh/{sujet}",
        data=f"{annonce['titre']}\n{prix} chez {annonce['vendeur']}".encode("utf-8"),
        headers={"Title": regle["nom"].encode("utf-8"), "Click": annonce["lien"]},
    )
    urllib.request.urlopen(requete, timeout=30).close()


def main():
    parser = argparse.ArgumentParser(description="Surveille les bons plans Dealabs.")
    parser.add_argument("--test", action="store_true", help="affiche sans alerter ni mémoriser")
    args = parser.parse_args()

    config = json.loads(FICHIER_CONFIG.read_text(encoding="utf-8"))
    vus = set(json.loads(FICHIER_VUS.read_text(encoding="utf-8"))) if FICHIER_VUS.exists() else set()

    meilleurs_prix = json.loads(FICHIER_PRIX.read_text(encoding="utf-8")) if FICHIER_PRIX.exists() else {}

    annonces = {}
    for url in FLUX:
        for annonce in lire_flux(url):
            annonces[annonce["lien"]] = annonce

    print(f"{len(annonces)} annonces lues")
    for lien, annonce in annonces.items():
        if lien in vus:
            continue
        for regle in config["surveillances"]:
            if correspond(annonce, regle):
                # Mémorise le meilleur prix vu par modèle pour repérer les baisses.
                modele = trouver_modele(annonce["titre"], regle)
                if modele and annonce["prix"] is not None:
                    ancien = meilleurs_prix.get(modele)
                    if ancien is not None and annonce["prix"] < ancien:
                        annonce["ancien_prix"] = ancien
                    if ancien is None or annonce["prix"] < ancien:
                        meilleurs_prix[modele] = annonce["prix"]
                print(f"[{regle['nom']}] {annonce['prix']} € — {annonce['titre']}\n  {lien}")
                if not args.test:
                    alerter(config["sujet_ntfy"], regle, annonce)
                break
        vus.add(lien)

    if not args.test:
        FICHIER_VUS.write_text(json.dumps(sorted(vus)), encoding="utf-8")
        FICHIER_PRIX.write_text(json.dumps(meilleurs_prix, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
