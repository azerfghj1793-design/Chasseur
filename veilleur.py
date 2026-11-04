"""VEILLEUR — surveille Dealabs et alerte sur Discord (ou ntfy), sans IA ni clé API.

Usage :
  python veilleur.py          vérifie les annonces et envoie les alertes
  python veilleur.py --test   affiche ce qui correspond, sans alerter ni mémoriser

Les produits et les prix maximum se règlent dans surveillances.json.
"""

import argparse
import json
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DOSSIER = Path(__file__).parent
FICHIER_CONFIG = DOSSIER / "surveillances.json"
FICHIER_VUS = DOSSIER / "deals_vus.json"
FICHIER_PRIX = DOSSIER / "meilleurs_prix.json"
FICHIER_JOURNAL = DOSSIER / "alertes.log"
FICHIER_ETAT = DOSSIER / "etat.json"
HEURE_RESUME = 20  # le résumé du jour part au premier passage après 20 h

# Flux lus à chaque passage. Les flux "groupe" ne contiennent que des TV OLED / écrans :
# ils rattrapent les deals sortis trop vite du flux général. On peut les remplacer
# en ajoutant une liste "flux" dans surveillances.json.
FLUX_PAR_DEFAUT = [
    "https://www.dealabs.com/rss/new",
    "https://www.dealabs.com/rss/hot",
    "https://www.dealabs.com/rss/groupe/tv-oled",
    "https://www.dealabs.com/rss/groupe/ecrans",
]
FLUX_CHAUD = "https://www.dealabs.com/rss/hot"
JOURS_MEMOIRE = 60  # les deals vus il y a plus longtemps sont oubliés
AGE_MAX_JOURS = 3   # un deal publié il y a plus longtemps est ignoré (souvent expiré)
# Phrases qui apparaissent sur la page Dealabs d'un deal expiré.
SIGNES_EXPIRE = ['"isExpired":true', "a expiré", "n'est plus d'actualité", "deal expiré"]

PEPPER = "{http://www.pepper.com/rss}merchant"
MEDIA = "{http://search.yahoo.com/mrss/}"
NAVIGATEUR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

COULEUR_NORMAL = 0x3498DB  # bleu
COULEUR_CHAUD = 0xE67E22   # orange
COULEUR_BAISSE = 0x2ECC71  # vert

# Pièges fréquents dans les titres Dealabs : le prix affiché n'est pas toujours
# celui que tu paies vraiment.
PIEGES = [
    (r"\bodr\b|rembours", "Prix après remboursement (ODR) : tu avances plus au départ"),
    (r"fid[ée]lit[ée]|ticket", "Une partie de la remise est en carte de fidélité ou en ticket"),
    (r"nouveau client", "Prix réservé aux nouveaux clients"),
    (r"via app", "Prix seulement via l'application"),
    (r"reconditionn|occasion|seconde main", "Produit reconditionné ou d'occasion"),
    (r"vendeur[s]? tiers|marketplace", "Vendeur tiers (pas le magasin lui-même)"),
    (r"retrait magasin|en magasin", "Seulement en magasin : vérifie le stock près de chez toi"),
    (r"\bpack\b", "Pack : le prix inclut d'autres produits"),
]


def lire_prix(texte: str | None) -> float | None:
    """'1 090,23€' → 1090.23"""
    if not texte:
        return None
    nombre = re.sub(r"[^\d,.]", "", texte).replace(".", "").replace(",", ".")
    try:
        return float(nombre)
    except ValueError:
        return None


def euros(prix: float) -> str:
    """1090.2 → '1 090,20 €'"""
    return f"{prix:,.2f} €".replace(",", " ").replace(".", ",")


def lire_image(item) -> str | None:
    for balise in ("content", "thumbnail"):
        media = item.find(MEDIA + balise)
        if media is not None and media.get("url"):
            return media.get("url")
    trouve = re.search(r'<img[^>]+src="([^"]+)"', item.findtext("description", "") or "")
    return trouve.group(1) if trouve else None


def lire_date(texte: str | None) -> datetime | None:
    """'Wed, 04 Nov 2026 16:20:00 +0100' → date utilisable"""
    try:
        return parsedate_to_datetime(texte) if texte else None
    except (TypeError, ValueError):
        return None


def trop_vieux(annonce: dict) -> bool:
    if annonce["publie"] is None:
        return False  # pas de date : on ne peut pas juger, on garde
    return datetime.now(timezone.utc) - annonce["publie"] > timedelta(days=AGE_MAX_JOURS)


def est_expire(lien: str) -> bool:
    """Ouvre la page du deal pour voir s'il est encore valable.

    Si la page ne s'ouvre pas, on considère le deal comme valable (mieux vaut
    une alerte de trop qu'une bonne affaire ratée).
    """
    try:
        requete = urllib.request.Request(lien, headers=NAVIGATEUR)
        with urllib.request.urlopen(requete, timeout=20) as reponse:
            page = reponse.read().decode("utf-8", errors="ignore")
    except Exception:
        return False
    return any(signe in page for signe in SIGNES_EXPIRE)


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
            "image": lire_image(item),
            "publie": lire_date(item.findtext("pubDate")),
            "chaud": url == FLUX_CHAUD,
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


def pieges(titre: str) -> list[str]:
    titre = titre.lower()
    return [texte for motif, texte in PIEGES if re.search(motif, titre)]


def motif_alerte(annonce: dict) -> tuple[str, int]:
    """Pourquoi on alerte : (phrase, couleur Discord)."""
    if annonce.get("baisse_deal"):
        return f"📉 Ce deal a baissé : {euros(annonce['baisse_deal'])} → {euros(annonce['prix'])}", COULEUR_BAISSE
    if annonce.get("record"):
        return f"🏆 Plus bas prix vu pour ce modèle (avant : {euros(annonce['record'])})", COULEUR_BAISSE
    if annonce["chaud"]:
        return "🔥 Deal chaud sur Dealabs", COULEUR_CHAUD
    return "🆕 Nouveau deal", COULEUR_NORMAL


def envoyer(url: str, donnees: bytes, entetes: dict):
    requete = urllib.request.Request(url, data=donnees, headers={**NAVIGATEUR, **entetes})
    urllib.request.urlopen(requete, timeout=30).close()


def alerter(sujet: str, regle: dict, annonce: dict):
    motif, couleur = motif_alerte(annonce)
    prix = euros(annonce["prix"]) if annonce["prix"] is not None else "non indiqué"
    a_savoir = pieges(annonce["titre"])

    # Une surveillance avec un webhook part sur Discord, les autres sur ntfy.
    if regle.get("webhook_discord"):
        champs = [
            {"name": "Prix", "value": prix, "inline": True},
            {"name": "Vendeur", "value": annonce["vendeur"], "inline": True},
            {"name": "Ton budget", "value": euros(regle["prix_max"]), "inline": True},
        ]
        if annonce["prix"] is not None:
            marge = regle["prix_max"] - annonce["prix"]
            champs.append({"name": "Sous ton budget de", "value": euros(marge), "inline": True})
        if a_savoir:
            champs.append({"name": "⚠️ À savoir", "value": "\n".join(f"• {t}" for t in a_savoir)})
        embed = {
            "author": {"name": regle["nom"]},
            "title": annonce["titre"][:256],
            "url": annonce["lien"],
            "description": motif,
            "color": couleur,
            "fields": champs,
            "footer": {"text": "Veilleur Dealabs · vérifie le prix avant d'acheter"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if annonce.get("image"):
            embed["thumbnail"] = {"url": annonce["image"]}
        envoyer(regle["webhook_discord"], json.dumps({"embeds": [embed]}).encode("utf-8"),
                {"Content-Type": "application/json"})
        return

    corps = f"{motif}\n{annonce['titre']}\n{prix} chez {annonce['vendeur']}"
    if a_savoir:
        corps += "\nÀ savoir : " + " / ".join(a_savoir)
    envoyer(f"https://ntfy.sh/{sujet}", corps.encode("utf-8"),
            {"Title": regle["nom"].encode("utf-8"), "Click": annonce["lien"]})


def journal(texte: str):
    """Ajoute une ligne datée dans alertes.log."""
    with FICHIER_JOURNAL.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} {texte}\n")


def alertes_du_jour() -> int:
    if not FICHIER_JOURNAL.exists():
        return 0
    debut = f"{date.today().isoformat()} "
    with FICHIER_JOURNAL.open(encoding="utf-8") as f:
        return sum(1 for ligne in f if ligne.startswith(debut) and " ALERTE " in ligne)


def webhooks(config: dict) -> list[str]:
    """Les webhooks Discord utilisés, sans doublon, dans l'ordre des surveillances."""
    return list(dict.fromkeys(r["webhook_discord"] for r in config["surveillances"] if r.get("webhook_discord")))


def message_discord(url: str, texte: str):
    envoyer(url, json.dumps({"content": texte[:2000]}).encode("utf-8"), {"Content-Type": "application/json"})


def signaler_etat(config: dict, etat: dict, probleme: str | None):
    """Prévient sur Discord quand le bot tombe en panne, puis quand il repart.

    Un seul message par panne, pour ne pas spammer toutes les 10 minutes.
    """
    if bool(probleme) == etat.get("en_panne", False):
        return
    texte = (f"⚠️ **Veilleur en panne** : {probleme}\nJe réessaie à chaque passage."
             if probleme else "✅ **Veilleur reparti** : tout fonctionne de nouveau.")
    journal("PANNE " + probleme if probleme else "REPARTI")
    cibles = webhooks(config)
    if not cibles:
        return
    try:
        message_discord(cibles[0], texte)
        etat["en_panne"] = bool(probleme)
    except Exception as erreur:
        # Discord injoignable : on note dans le journal et on réessaiera.
        journal(f"ERREUR message de panne non envoyé ({erreur})")


def envoyer_resume(config: dict, meilleurs_prix: dict):
    """Un message par salon Discord : le meilleur prix vu pour chaque modèle suivi."""
    par_salon = {}
    for regle in config["surveillances"]:
        if not regle.get("modeles") or not regle.get("webhook_discord"):
            continue
        lignes = [f"**{regle['nom']}** (budget {euros(regle['prix_max'])})"]
        for modele in regle["modeles"]:
            prix = meilleurs_prix.get(modele)
            lignes.append(f"• {modele} : {euros(prix) if prix is not None else 'pas encore vu'}")
        par_salon.setdefault(regle["webhook_discord"], []).append("\n".join(lignes))

    entete = f"📊 **Résumé du {date.today():%d/%m}** · {alertes_du_jour()} alerte(s) aujourd'hui\n"
    for url, blocs in par_salon.items():
        message_discord(url, entete + "\n\n".join(blocs) + "\n\n_Meilleurs prix vus sur Dealabs depuis le début du suivi._")
    journal("RESUME envoyé")


def charger_vus() -> dict:
    """Mémoire des deals : {lien: {"date": ..., "prix": ..., "alerte": bool}}."""
    if not FICHIER_VUS.exists():
        return {}
    donnees = json.loads(FICHIER_VUS.read_text(encoding="utf-8"))
    aujourd_hui = date.today().isoformat()
    if isinstance(donnees, list):
        # Ancien format (simple liste de liens) : on les compte comme déjà traités.
        return {lien: {"date": aujourd_hui, "alerte": True} for lien in donnees}
    limite = (date.today() - timedelta(days=JOURS_MEMOIRE)).isoformat()
    return {lien: info for lien, info in donnees.items() if info.get("date", aujourd_hui) >= limite}


def main():
    parser = argparse.ArgumentParser(description="Surveille les bons plans Dealabs.")
    parser.add_argument("--test", action="store_true", help="affiche sans alerter ni mémoriser")
    parser.add_argument("--resume", action="store_true", help="envoie le résumé du jour tout de suite")
    args = parser.parse_args()

    config = json.loads(FICHIER_CONFIG.read_text(encoding="utf-8"))
    # Dans le cloud, les webhooks ne sont pas écrits dans le fichier :
    # "env:WEBHOOK_TV" veut dire "lis la variable d'environnement WEBHOOK_TV".
    for regle in config["surveillances"]:
        if str(regle.get("webhook_discord", "")).startswith("env:"):
            regle["webhook_discord"] = os.environ.get(regle["webhook_discord"][4:], "")
    etat = json.loads(FICHIER_ETAT.read_text(encoding="utf-8")) if FICHIER_ETAT.exists() else {}
    vus = charger_vus()
    meilleurs_prix = json.loads(FICHIER_PRIX.read_text(encoding="utf-8")) if FICHIER_PRIX.exists() else {}
    aujourd_hui = date.today().isoformat()

    # Lecture des flux : si un flux plante, on continue avec les autres.
    annonces = {}
    flux = config.get("flux", FLUX_PAR_DEFAUT)
    flux_ko = []
    for url in flux:
        try:
            lues = lire_flux(url)
        except Exception as erreur:
            print(f"[attention] flux illisible, ignoré : {url} ({erreur})")
            flux_ko.append(url)
            if not args.test:
                journal(f"ERREUR flux illisible {url} ({erreur})")
            continue
        print(f"{len(lues):>3} annonces · {url}")
        for annonce in lues:
            if annonce["lien"] in annonces:
                annonces[annonce["lien"]]["chaud"] |= annonce["chaud"]
            else:
                annonces[annonce["lien"]] = annonce

    alertes, echecs = 0, 0
    for lien, annonce in annonces.items():
        regle = next((r for r in config["surveillances"] if correspond(annonce, r)), None)
        deja = vus.get(lien)

        if regle is None:
            vus.setdefault(lien, {"date": aujourd_hui})
            continue

        if trop_vieux(annonce):
            vus.setdefault(lien, {"date": aujourd_hui})
            print(f"\n[ignoré : publié il y a plus de {AGE_MAX_JOURS} jours] {annonce['titre']}")
            continue

        if est_expire(lien):
            vus[lien] = {"date": aujourd_hui, "alerte": True}  # inutile de revérifier
            print(f"\n[ignoré : deal expiré] {annonce['titre']}")
            continue

        if deja and deja.get("alerte"):
            # Déjà alerté : on ne réalerte que si le prix de ce deal a baissé.
            ancien = deja.get("prix")
            if ancien is None or annonce["prix"] is None or annonce["prix"] >= ancien:
                continue
            annonce["baisse_deal"] = ancien

        # Record de prix par modèle suivi.
        modele = trouver_modele(annonce["titre"], regle)
        if modele and annonce["prix"] is not None:
            record = meilleurs_prix.get(modele)
            if record is not None and annonce["prix"] < record:
                annonce["record"] = record
            if record is None or annonce["prix"] < record:
                meilleurs_prix[modele] = annonce["prix"]

        motif, _ = motif_alerte(annonce)
        prix = euros(annonce["prix"]) if annonce["prix"] is not None else "prix ?"
        print(f"\n[{regle['nom']}] {motif}\n  {prix} — {annonce['titre']}\n  {lien}")
        for texte in pieges(annonce["titre"]):
            print(f"  ⚠️ {texte}")

        if args.test:
            continue
        try:
            alerter(config["sujet_ntfy"], regle, annonce)
        except Exception as erreur:
            # Pas mémorisé : on réessaiera au prochain passage.
            print(f"  [erreur] alerte non envoyée ({erreur}), nouvel essai au prochain passage")
            journal(f"ERREUR alerte non envoyée [{regle['nom']}] {annonce['titre']} ({erreur})")
            echecs += 1
            continue
        journal(f"ALERTE [{regle['nom']}] {prix} — {annonce['titre']} — {lien}")
        alertes += 1
        vus[lien] = {"date": (deja or {}).get("date", aujourd_hui), "prix": annonce["prix"], "alerte": True}
        time.sleep(1)  # évite de se faire bloquer par Discord si plusieurs alertes d'un coup

    print(f"\n{len(annonces)} annonces lues · {alertes} alerte(s) envoyée(s)"
          + (f" · {echecs} échec(s)" if echecs else ""))

    if args.test:
        return

    FICHIER_VUS.write_text(json.dumps(vus, ensure_ascii=False), encoding="utf-8")
    FICHIER_PRIX.write_text(json.dumps(meilleurs_prix, indent=2), encoding="utf-8")

    # Panne : aucun flux lisible, ou des alertes qui ne partent pas.
    probleme = None
    if len(flux_ko) == len(flux):
        probleme = "Dealabs est illisible (aucun flux n'a répondu)"
    elif echecs:
        probleme = f"{echecs} alerte(s) n'ont pas pu partir sur Discord"
    signaler_etat(config, etat, probleme)

    # Résumé quotidien : une fois par jour, après HEURE_RESUME.
    deja_envoye = etat.get("dernier_resume") == date.today().isoformat()
    if args.resume or (datetime.now().hour >= HEURE_RESUME and not deja_envoye):
        try:
            envoyer_resume(config, meilleurs_prix)
            etat["dernier_resume"] = date.today().isoformat()
        except Exception as erreur:
            journal(f"ERREUR résumé non envoyé ({erreur})")

    FICHIER_ETAT.write_text(json.dumps(etat, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
