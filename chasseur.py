"""CHASSEUR — trouve les meilleures offres en ligne pour n'importe quel produit.

Usage :
  python chasseur.py "écran OLED 27 pouces 240 Hz, max 700 €"

Principe : Claude cherche sur le web, puis rend ses offres via l'outil
`rendre_offres`. Le code garde ensuite le contrôle : une offre dont le site
n'est jamais apparu dans les recherches est rejetée, puis le reste est classé
par prix total livré.
"""

import argparse
import os
import sys
from datetime import date
from urllib.parse import urlparse

import anthropic
from dotenv import load_dotenv

# Le .env est cherché à partir du dossier de ce fichier.
load_dotenv()

MODELE = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_TOURS = 8  # garde-fou : nombre maximum d'appels à l'API par recherche

SYSTEM_PROMPT = """Tu es un chasseur de bonnes affaires pour un acheteur en France.
Tu cherches sur le web les meilleures offres actuelles pour le produit demandé.

Règles :
- Vérifie chaque offre sur la page du vendeur quand c'est possible.
- N'invente jamais un prix ni un lien. Si le prix n'est pas confirmé, mets la confiance à "faible".
- Vise le modèle exact : signale les variantes (taille, année, Hz) dans le champ produit.
- prix_total inclut la livraison vers la France ; ignore les remboursements différés (ODR) et mentionne-les dans remarques.
- Distingue le vendeur direct d'un vendeur tiers de marketplace, et le neuf du reconditionné.
- Si la demande vise une catégorie large, compare les modèles pertinents.

Quand tu as fini, appelle l'outil rendre_offres : c'est ta seule façon de répondre."""

OUTIL_RENDU = {
    "name": "rendre_offres",
    "description": "Rend la liste finale des offres trouvées et la recommandation.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["offres", "recommandation"],
        "properties": {
            "recommandation": {
                "type": "string",
                "description": "Conseil final en 2-3 phrases, y compris s'il vaut mieux attendre.",
            },
            "offres": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["produit", "prix_total", "vendeur", "vendeur_type",
                                 "etat", "lien", "confiance", "remarques"],
                    "properties": {
                        "produit": {"type": "string"},
                        "prix_total": {"type": "number", "description": "En euros, livraison incluse."},
                        "vendeur": {"type": "string"},
                        "vendeur_type": {"type": "string", "enum": ["direct", "marketplace", "inconnu"]},
                        "etat": {"type": "string", "enum": ["neuf", "reconditionne", "occasion"]},
                        "lien": {"type": "string"},
                        "confiance": {"type": "string", "enum": ["haute", "moyenne", "faible"]},
                        "remarques": {"type": "string"},
                    },
                },
            },
        },
    },
}

OUTILS = [
    {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 8,
        "user_location": {"type": "approximate", "country": "FR", "timezone": "Europe/Paris"},
    },
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10},
    OUTIL_RENDU,
]


def domaine(url: str) -> str:
    """https://www.fnac.com/a/1 → fnac.com"""
    hote = (urlparse(url).hostname or "").lower()
    return hote.removeprefix("www.")


def domaines_vus(contenu) -> set[str]:
    """Récupère les sites réellement renvoyés par les outils web."""
    vus = set()
    for bloc in contenu:
        if bloc.type == "web_search_tool_result" and isinstance(bloc.content, list):
            vus.update(domaine(r.url) for r in bloc.content)
        elif bloc.type == "web_fetch_tool_result" and getattr(bloc.content, "url", None):
            vus.add(domaine(bloc.content.url))
    return vus


def filtrer_offres(offres: list[dict], vus: set[str]) -> tuple[list[dict], int]:
    """Rejette les offres venues d'un site non vu, classe par prix. Renvoie (gardées, nb rejetées)."""
    gardees = [o for o in offres if domaine(o["lien"]) in vus and o["prix_total"] > 0]
    gardees.sort(key=lambda o: o["prix_total"])
    return gardees, len(offres) - len(gardees)


def chercher(demande: str) -> tuple[dict, set[str]]:
    """La boucle : Claude cherche jusqu'à appeler rendre_offres."""
    client = anthropic.Anthropic()
    messages = [{
        "role": "user",
        "content": f"Date du jour : {date.today().isoformat()}\nDemande : {demande}",
    }]
    vus = set()

    for _ in range(MAX_TOURS):
        with client.beta.messages.stream(
            model=MODELE,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=OUTILS,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=messages,
        ) as flux:
            reponse = flux.get_final_message()

        if reponse.stop_reason == "refusal":
            raise RuntimeError("Claude a refusé cette demande.")

        vus |= domaines_vus(reponse.content)
        messages.append({"role": "assistant", "content": reponse.content})

        for bloc in reponse.content:
            if bloc.type == "tool_use" and bloc.name == "rendre_offres":
                return bloc.input, vus

        if reponse.stop_reason != "pause_turn":
            # Claude s'est arrêté sans rendre ses offres : on le lui rappelle.
            messages.append({"role": "user", "content": "Appelle maintenant rendre_offres."})

    raise RuntimeError(f"Pas de résultat après {MAX_TOURS} tours.")


def afficher(resultat: dict, gardees: list[dict], rejetees: int):
    if not gardees:
        print("\nAucune offre vérifiable trouvée.")
    for i, o in enumerate(gardees, 1):
        print(f"\n{i}. {o['prix_total']:.2f} € — {o['produit']}")
        print(f"   {o['vendeur']} ({o['vendeur_type']}) · {o['etat']} · confiance {o['confiance']}")
        if o["remarques"]:
            print(f"   {o['remarques']}")
        print(f"   {o['lien']}")
    if rejetees:
        print(f"\n[{rejetees} offre(s) rejetée(s) : site non vu dans les recherches]")
    print(f"\nRecommandation : {resultat['recommandation']}")
    print(f"Prix relevés le {date.today().isoformat()} : à reconfirmer avant d'acheter.")


def main():
    parser = argparse.ArgumentParser(description="Trouve les meilleures offres pour un produit.")
    parser.add_argument("demande", help='ex : "écran OLED 27 pouces 240 Hz, max 700 €"')
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("[erreur] Clé API absente. Mets-la dans Chasseur/.env.")

    print("Recherche en cours (1 à 3 minutes)...")
    try:
        resultat, vus = chercher(args.demande)
    except anthropic.AuthenticationError:
        sys.exit("[erreur] Clé API invalide ou absente. Vérifie Chasseur/.env.")
    except anthropic.RateLimitError:
        sys.exit("[erreur] Trop de requêtes. Réessaie dans un instant.")
    except anthropic.BadRequestError as e:
        sys.exit(f"[erreur] Requête refusée par l'API : {e.message}")
    except anthropic.APIStatusError as e:
        sys.exit(f"[erreur] Problème côté Anthropic (code {e.status_code}).")
    except anthropic.APIConnectionError:
        sys.exit("[erreur] Impossible de joindre l'API. Vérifie ta connexion.")
    except RuntimeError as e:
        sys.exit(f"[erreur] {e}")

    gardees, rejetees = filtrer_offres(resultat["offres"], vus)
    afficher(resultat, gardees, rejetees)


if __name__ == "__main__":
    main()
