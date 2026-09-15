# -*- coding: utf-8 -*-
"""
Enrichissement SIRET + dirigeant via l'API recherche-entreprises.api.gouv.fr

Principe : on n'envoie JAMAIS l'adresse dans le parametre `q`.
Le nom nettoye va dans `q`, la geographie va dans les filtres dedies
(code_postal / departement), ce qui est la seule facon d'obtenir des
resultats fiables.
"""

import csv
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import ssl

# --- CONFIGURATION ---
FICHIER_ENTREE = "entreprises.csv"
FICHIER_SORTIE = "resultats_complets.tsv"

PAUSE_ENTRE_APPELS = 0.35     # l'API plafonne a 7 req/s
MAX_TENTATIVES_429 = 4        # nombre de reessais sur quota depasse
DEBUG = True                  # True = affiche chaque URL testee

API_BASE = "https://recherche-entreprises.api.gouv.fr/search"

# Mots vides / marketing a retirer du nom avant interrogation
MOTS_PARASITES = {
    "massages", "massage", "bien", "etre", "être", "wellness", "spa",
    "centre", "complexe", "base", "loisirs", "aquatique", "nautique",
    "piscine", "hotel", "hôtel", "gite", "gîte", "camping", "aventure",
    "canyoning", "tyroliennes", "ferrata", "via", "parc", "club", "forme",
    "domicile", "entreprises", "therapeute", "thérapeute", "holistique",
    "sonotherapeute", "sonothérapeute", "constellatrice", "masseuse",
    "masseur", "cure", "thermale", "chambres", "hotes", "hôtes",
    "location", "maison", "vacances", "nature", "experience", "expérience",
}


# ----------------------------------------------------------------------
# NETTOYAGE
# ----------------------------------------------------------------------

def nettoyer_texte(valeur):
    """Normalise les espaces et retire les caracteres de controle."""
    if not valeur:
        return ""
    valeur = valeur.replace("\u2019", "'").replace("\u2018", "'")
    valeur = re.sub(r"[\x00-\x1f\x7f]", " ", valeur)
    return re.sub(r"\s+", " ", valeur).strip()


def nettoyer_nom_societe(nom):
    """
    Transforme un nom issu d'un scrap Google Maps en requete exploitable.

    "HARMONIA Massages Bien-etre - Marmande"  -> "HARMONIA"
    "Accroche Toi Aux Branches | Vallon..."   -> "Accroche Toi Aux Branches"
    "Thermes De Digne Les Bains (cure...)"    -> "Thermes De Digne Les Bains"
    """
    nom = nettoyer_texte(nom)

    # 1. On coupe a partir du premier separateur de "baseline" marketing
    for sep in ["|", " - ", " – ", " — ", " : ", " / "]:
        if sep in nom:
            nom = nom.split(sep)[0].strip()

    # 2. On retire les parentheses et leur contenu
    nom = re.sub(r"\([^)]*\)", " ", nom)

    # 3. On retire les virgules (souvent "Enseigne, Prenom Nom : description")
    if "," in nom:
        nom = nom.split(",")[0].strip()

    # 4. Ponctuation residuelle
    nom = re.sub(r"[«»\"“”]", " ", nom)
    nom = nettoyer_texte(nom)

    return nom


def nom_raccourci(nom, n_mots=3):
    """Garde les n premiers mots significatifs (derniere chance)."""
    mots = [m for m in nom.split() if len(m) > 2]
    significatifs = [m for m in mots if m.lower().strip("'-") not in MOTS_PARASITES]
    base = significatifs if significatifs else mots
    return " ".join(base[:n_mots])


def extraire_code_postal(adresse):
    """Recupere un code postal francais a 5 chiffres dans l'adresse."""
    if not adresse:
        return ""
    m = re.search(r"\b(\d{5})\b", adresse)
    return m.group(1) if m else ""


def extraire_departement(code_postal):
    """20xxx -> 2A/2B non geres finement ; on renvoie les 2 premiers chiffres."""
    if not code_postal or len(code_postal) != 5:
        return ""
    if code_postal.startswith("97") or code_postal.startswith("98"):
        return code_postal[:3]
    if code_postal.startswith("20"):
        # L'API attend 2A / 2B pour la Corse et le decoupage ne suit pas
        # strictement le code postal : on prefere ne pas filtrer.
        return ""
    return code_postal[:2]


def extraire_ville(adresse, code_postal):
    """Prend ce qui suit le code postal comme nom de ville."""
    if not adresse or not code_postal:
        return ""
    partie = adresse.split(code_postal, 1)
    if len(partie) < 2:
        return ""
    ville = nettoyer_texte(partie[1])
    ville = re.sub(r"\b(France|FRANCE)\b", "", ville)
    ville = ville.strip(" ,-")
    return ville


# ----------------------------------------------------------------------
# APPEL API
# ----------------------------------------------------------------------

def _contexte_ssl():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def appeler_api(params):
    """
    Effectue un appel et renvoie (data, message_erreur).
    Gere le 429 avec un backoff exponentiel.
    """
    url = API_BASE + "?" + urllib.parse.urlencode(params)

    if DEBUG:
        print(f"      ? {url}")

    for tentative in range(MAX_TENTATIVES_429):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "EnrichissementSIRET/2.0"}
            )
            with urllib.request.urlopen(req, context=_contexte_ssl(), timeout=15) as r:
                return json.loads(r.read().decode("utf-8")), None

        except urllib.error.HTTPError as e:
            if e.code == 429:
                attente = 2 ** tentative
                print(f"      [429] quota atteint, pause de {attente}s...")
                time.sleep(attente)
                continue
            if e.code == 400:
                # requete invalide : inutile de reessayer
                return None, "HTTP 400 (requete invalide)"
            return None, f"HTTP {e.code}"

        except urllib.error.URLError as e:
            return None, f"Reseau : {e.reason}"

        except Exception as e:
            return None, f"{type(e).__name__} : {e}"

    return None, "HTTP 429 (quota, abandon apres reessais)"


def extraire_siret_dirigeant(data):
    """Extrait le SIRET et le dirigeant principal du premier resultat."""
    resultats = data.get("results") or []
    if not resultats:
        return None, None

    premier = resultats[0]

    # SIRET : etablissement correspondant, sinon siege
    siret = None
    etabs = premier.get("matching_etablissements") or []
    if etabs:
        siret = etabs[0].get("siret")
    if not siret:
        siret = (premier.get("siege") or {}).get("siret")

    # Dirigeant principal
    dirigeant = None
    dirs = premier.get("dirigeants") or []
    if dirs:
        d = dirs[0]
        if d.get("nom") or d.get("prenoms"):
            nom_p = (d.get("nom") or "").upper()
            prenom_p = (d.get("prenoms") or "").split(" ")[0].title()
            dirigeant = f"{prenom_p} {nom_p}".strip()
        elif d.get("denomination"):
            dirigeant = d.get("denomination")

    # Nom legal trouve, utile pour controler la pertinence
    nom_legal = premier.get("nom_complet") or premier.get("nom_raison_sociale") or ""

    return (siret, dirigeant), nom_legal


def chercher_entreprise(nom_brut, adresse=""):
    """
    Cascade de strategies, de la plus precise a la plus large.
    Renvoie (siret, dirigeant, nom_legal, strategie_gagnante).
    """
    nom = nettoyer_nom_societe(nom_brut)
    if not nom:
        return "Nom vide", "Nom vide", "", "-"

    cp = extraire_code_postal(adresse)
    dept = extraire_departement(cp)
    ville = extraire_ville(adresse, cp)
    court = nom_raccourci(nom)

    strategies = []
    if cp:
        strategies.append(("nom + code postal", {"q": nom, "code_postal": cp}))
    if dept:
        strategies.append(("nom + departement", {"q": nom, "departement": dept}))
    if ville:
        strategies.append(("nom + ville", {"q": f"{nom} {ville}"}))
    strategies.append(("nom seul", {"q": nom}))
    if court and court.lower() != nom.lower():
        if dept:
            strategies.append(("nom court + dept", {"q": court, "departement": dept}))
        strategies.append(("nom court", {"q": court}))

    derniere_erreur = None

    for libelle, params in strategies:
        params = dict(params)
        params["per_page"] = 1
        params["limite_matching_etablissements"] = 1

        data, erreur = appeler_api(params)
        time.sleep(PAUSE_ENTRE_APPELS)

        if erreur:
            derniere_erreur = erreur
            continue

        resultat, nom_legal = extraire_siret_dirigeant(data)
        if resultat:
            siret, dirigeant = resultat
            return (
                siret or "SIRET absent",
                dirigeant or "Dirigeant non publie",
                nom_legal,
                libelle,
            )

    if derniere_erreur:
        return f"Erreur : {derniere_erreur}", "Erreur", "", "-"
    return "Non trouve", "Non trouve", "", "-"


# ----------------------------------------------------------------------
# LECTURE DU FICHIER
# ----------------------------------------------------------------------

def lire_fichier(chemin):
    """
    Lit le CSV/TSV en devinant l'encodage puis le separateur.
    On teste utf-8-sig d'abord : c'est le cas le plus frequent et
    `errors="ignore"` avec cp1252 detruit silencieusement des caracteres.
    """
    contenu = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            with open(chemin, "r", encoding=enc) as f:
                contenu = f.read()
            print(f"-> Encodage retenu : {enc}")
            break
        except UnicodeDecodeError:
            continue

    if contenu is None:
        raise RuntimeError("Impossible de decoder le fichier.")

    # Detection du separateur : on compte les occurrences plutot que
    # de prendre le premier trouve (un seul nom avec virgule suffisait
    # a faire basculer l'ancienne detection).
    premiere_ligne = contenu.split("\n", 1)[0]
    scores = {sep: premiere_ligne.count(sep) for sep in ["\t", ";", ","]}
    separateur = max(scores, key=scores.get)
    if scores[separateur] == 0:
        separateur = "\t"

    affichage = {"\t": "\\t", ";": ";", ",": ","}[separateur]
    print(f"-> Separateur retenu : '{affichage}'")

    return list(csv.reader(contenu.splitlines(), delimiter=separateur))


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    print(f"Debut du traitement de '{FICHIER_ENTREE}'...\n")

    try:
        lignes = lire_fichier(FICHIER_ENTREE)
    except FileNotFoundError:
        print(f"Erreur : le fichier '{FICHIER_ENTREE}' est introuvable.")
        return
    except RuntimeError as e:
        print(f"Erreur : {e}")
        return

    traitees = 0
    trouvees = 0

    with open(FICHIER_SORTIE, "w", encoding="utf-8-sig", newline="") as f_out:
        ecrivain = csv.writer(f_out, delimiter="\t")
        ecrivain.writerow(
            ["Nom d'origine", "Nom interroge", "SIRET", "Dirigeant",
             "Nom legal trouve", "Strategie"]
        )

        for ligne in lignes:
            if not ligne or not ligne[0].strip():
                continue

            nom_societe = nettoyer_texte(ligne[0])

            if nom_societe.lower() in ("nom_societe", "nom societe",
                                       "nom société", "nom", "entreprise"):
                continue

            adresse = ""
            if len(ligne) >= 2:
                candidat = nettoyer_texte(ligne[1])
                if "Erreur" not in candidat and "Non trouv" not in candidat:
                    adresse = candidat

            print(f"[{traitees + 1}] {nom_societe}")

            siret, dirigeant, nom_legal, strategie = chercher_entreprise(
                nom_societe, adresse
            )

            if siret and siret[0].isdigit():
                trouvees += 1
                print(f"    OK  SIRET {siret} | {dirigeant} | via {strategie}")
                if nom_legal:
                    print(f"        nom legal : {nom_legal}")
            else:
                print(f"    KO  {siret}")

            ecrivain.writerow([
                nom_societe,
                nettoyer_nom_societe(nom_societe),
                siret,
                dirigeant,
                nom_legal,
                strategie,
            ])
            f_out.flush()
            traitees += 1

    taux = (trouvees / traitees * 100) if traitees else 0
    print(f"\nTermine : {trouvees}/{traitees} trouvees ({taux:.0f} %).")
    print(f"Resultat enregistre dans '{FICHIER_SORTIE}'.")


if __name__ == "__main__":
    main()