# -*- coding: utf-8 -*-
import os
import re
import sys
import time
import random
import threading
import unicodedata
import multiprocessing as mp
from urllib.parse import urlparse
import pandas as pd

# Sortie non bufferisée : sans ça, les print() peuvent rester invisibles dans les
# logs GitHub Actions pendant un long moment (la sortie n'est pas un terminal),
# donnant l'impression à tort que le script est bloqué alors qu'il avance.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# Import natif basé sur votre exemple de script
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

class RechercheDDGSExpiree(Exception):
    """Levée quand un appel DDGS dépasse la durée maximale autorisée (voir
    ddgs_text_avec_timeout) — utile car DuckDuckGo peut, sur les IPs partagées de
    GitHub Actions, ne renvoyer aucune réponse ni erreur et bloquer indéfiniment."""
    pass

def _worker_ddgs(query, kwargs, resultat_queue):
    """Exécuté dans un processus séparé (voir ddgs_text_avec_timeout)."""
    try:
        with DDGS() as ddgs:
            resultats = list(ddgs.text(query, **kwargs))
        resultat_queue.put(("ok", resultats))
    except Exception as e:
        resultat_queue.put(("erreur", str(e)))

def ddgs_text_avec_timeout(query, timeout=20, **kwargs):
    """
    Exécute une recherche DDGS dans un PROCESSUS séparé, avec une limite de temps
    stricte. Si le processus ne répond pas à temps, il est tué de force
    (Process.terminate) — contrairement à un simple timeout par signal, ceci
    fonctionne même si l'appel réseau est bloqué dans du code natif (le client
    HTTP de la librairie ddgs est écrit en Rust) qui ignore les signaux Python.

    Par défaut, restreint les moteurs interrogés à une liste fiable/joignable
    depuis les runners GitHub Actions : le mode "auto" de ddgs essaie Wikipedia et
    Grokipedia en premier (hors-sujet ici, et Grokipedia est injoignable en
    pratique : "Network is unreachable"), puis Google, lui aussi injoignable
    depuis ces IPs. On évite ce gaspillage de temps en ciblant directement les
    moteurs qui répondent réellement.
    """
    kwargs.setdefault("backend", "duckduckgo,bing,brave,mojeek,startpage,yahoo")
    resultat_queue = mp.Queue()
    processus = mp.Process(target=_worker_ddgs, args=(query, kwargs, resultat_queue))
    processus.daemon = True
    processus.start()
    processus.join(timeout)

    if processus.is_alive():
        processus.terminate()
        processus.join(5)
        if processus.is_alive():
            processus.kill()
            processus.join()
        raise RechercheDDGSExpiree(
            f"Délai de {timeout}s dépassé en attendant la réponse de DuckDuckGo "
            f"(processus de recherche arrêté de force)."
        )

    if resultat_queue.empty():
        raise RechercheDDGSExpiree("Le processus de recherche s'est arrêté sans renvoyer de résultat.")

    statut, valeur = resultat_queue.get()
    if statut == "erreur":
        raise Exception(valeur)
    return valeur

# ==========================================
# CONFIGURATION
# ==========================================
FICHIER_ENTREE = "liste_urls.csv"
FICHIER_SORTIE = "profils_linkedin_trouves.csv"
FICHIER_DEBUG_ECARTES = "profils_ecartes_debug.csv"

POSTES_CIBLES = [
    "Directeur d'établissement",
    "Manager d'établissement",
    "Directeur équipements",
    "Responsable équipements",
    "Dirigeant",
    "Directeur général",
    "Gérant",
    "Président",
    "Fondateur",
    "Responsable Spa",
    "Spa Manager",
    "Directeur Spa",
    "Responsable Thalasso",
    "Directeur Thalasso",
    "Responsable Wellness",
    "Responsable Bien-être",
    "Responsable Balnéo",
]

# Mots-clés servant à repérer, dans l'intitulé RÉEL trouvé (pas le poste recherché), si la
# personne occupe un poste de dirigeant / haute responsabilité — quel que soit le poste qui
# a permis de la trouver au départ.
MOTS_CLES_DIRIGEANT = [
    "directeur general", "directrice generale", "dirigeant", "dirigeante",
    "gerant", "gerante", "president", "presidente", "pdg", "ceo", "dg",
    "fondateur", "fondatrice", "cofondateur", "cofondatrice", "co fondateur",
    "co fondatrice", "proprietaire", "responsable general", "responsable generale",
    "directeur des operations", "directrice des operations", "chief executive",
    "managing director", "general manager", "directeur d etablissement",
    "directrice d etablissement", "directeur de l etablissement",
]

# Mots-clés servant à repérer, dans l'intitulé RÉEL trouvé, un poste lié aux équipements
# bien-être (spa, thalasso, wellness, balnéo, thermes...), quel que soit le poste recherché
# qui a permis de trouver la personne.
MOTS_CLES_EQUIPEMENTS_BIEN_ETRE = [
    "spa", "thalasso", "thalassotherapie", "wellness", "bien etre", "balneo",
    "balneotherapie", "thermal", "thermes", "piscine", "fitness", "massage",
    "esthetique", "estheticien", "hammam", "sauna", "beaute",
]

# Nombre d'entreprises traitées par exécution (utile pour GitHub Actions,
# afin de rester sous la limite de temps d'un job et de relancer en boucle).
# BATCH_SIZE=0 (ou variable absente) => traite tout le fichier en une seule fois.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "0") or "0")

# Mots indiquant que le poste n'est probablement plus d'actualité
INDICES_ANCIEN_POSTE = ["ancien", "ex-", "ex ", "former", "etait", "a quitte", "ancienne"]

# Pour détecter une période d'emploi du type "sept. 2025 - aujourd'hui · 1 an 1 mois"
# dans la description (body) du résultat de recherche.
MOIS_ABREV = r"(?:janv|f[ée]vr|mars|avr|mai|juin|juil|ao[ûu]t|sept|oct|nov|d[ée]c)\.?"
PATTERN_PERIODE = re.compile(
    rf"({MOIS_ABREV}\.?\s*\d{{4}}|\d{{4}})\s*[-–—]\s*"
    rf"({MOIS_ABREV}\.?\s*\d{{4}}|\d{{4}}|aujourd'?hui|present|présent)"
    r"(\s*[·•]\s*\d+\s*an[s]?(?:\s*\d+\s*mois)?|\s*[·•]\s*\d+\s*mois)?",
    re.IGNORECASE
)
MOTS_PERIODE_EN_COURS = ["aujourd'hui", "aujourdhui", "present", "présent"]

# Mots trop génériques dans le secteur hôtellerie/spa/tourisme pour servir, à eux seuls,
# de preuve de correspondance entre deux noms d'entreprise (ex. "spa" ou "domaine" se
# retrouvent dans des dizaines d'établissements différents).
MOTS_GENERIQUES_ENTREPRISE = {
    "hotel", "hôtel", "spa", "domaine", "groupe", "group", "resort", "resorts",
    "thermes", "thermal", "thermale", "thermes", "wellness", "tourisme", "tourism",
    "office", "france", "com", "sarl", "sas", "château", "chateau", "restaurant",
}

WRITE_LOCK = threading.Lock()

# Domaines à ignorer quand on cherche le SITE OFFICIEL d'une entreprise (annuaires,
# réseaux sociaux, plateformes d'avis... ce ne sont jamais le site officiel).
DOMAINES_EXCLUS_SITE = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "wikipedia.org", "pagesjaunes.fr", "societe.com", "google.com", "google.fr",
    "tripadvisor.com", "tripadvisor.fr", "indeed.com", "glassdoor.fr", "glassdoor.com",
    "viadeo.com", "youtube.com", "pinterest.com", "yelp.com", "yelp.fr",
}

# Détection d'un numéro de téléphone français dans un extrait de recherche
# (ex: "01 23 45 67 89", "+33 1 23 45 67 89", "01.23.45.67.89").
PHONE_REGEX = re.compile(r'(?:\+33[\s.\-]?|0)[1-9](?:[\s.\-]?\d{2}){4}')

# Détection approximative d'une adresse française (numéro + rue + code postal + ville)
# dans un extrait de recherche. Reste imprécis par nature (dépend de ce que
# DuckDuckGo a indexé) : à vérifier manuellement en cas de doute.
ADRESSE_PATTERN = re.compile(
    r'([0-9]{1,4}[^,\n|]{2,70}?,?\s*\d{5}\s+[A-ZÀ-Ü][A-Za-zÀ-ÿ\'\-\s]{1,40})'
)

# Mots-clés utilisés pour repérer, dans les en-têtes du fichier d'entrée, des colonnes
# déjà existantes de site web / adresse / téléphone (pour ne PAS relancer une
# recherche si l'info est déjà présente).
MOTS_CLES_COL_SITE = ["site web", "siteweb", "site internet", "website"]
MOTS_CLES_COL_ADRESSE = ["adresse", "address"]
MOTS_CLES_COL_TELEPHONE = ["telephone", "tel", "phone"]
MOTS_CLES_COL_NOM = ["nom entreprise", "nom societe", "raison sociale", "nom company", "societe", "entreprise", "company", "nom"]

# ==========================================
# FONCTIONS UTILITAIRES (Inspirées de votre exemple)
# ==========================================

def read_table_with_format(path):
    """Lit le fichier CSV avec détection automatique des encodages et séparateurs,
    et renvoie aussi l'encodage/séparateur détectés (pour pouvoir réécrire le fichier
    dans le même format).

    IMPORTANT : lecture forcée en texte (dtype=str, keep_default_na=False) pour éviter
    qu'une colonne entièrement vide (ex. "Traite" avant tout traitement) soit interprétée
    par pandas comme un type numérique (float/NaN), ce qui provoquerait une erreur
    ("Invalid value 'Oui' for dtype 'float64'") dès qu'on y écrit du texte ensuite.

    On teste TOUTES les combinaisons encodage/séparateur et on garde celle qui donne
    le PLUS de colonnes (et non la première qui "réussit" techniquement) : un séparateur
    incorrect réussit souvent à lire le fichier mais en 1 seule colonne fourre-tout,
    ce qui corrompait silencieusement le fichier de sortie lors des ré-écritures.
    """
    trials = [("utf-8", ","), ("utf-8", ";"), ("utf-8", "\t"), ("utf-8-sig", ","), ("utf-8-sig", ";"),
              ("utf-8-sig", "\t"), ("cp1252", ","), ("cp1252", ";"), ("cp1252", "\t")]
    meilleur = None
    for enc, sep in trials:
        try:
            df = pd.read_csv(path, encoding=enc, sep=sep, dtype=str, keep_default_na=False)
            if len(df.columns) >= 1:
                if meilleur is None or len(df.columns) > len(meilleur[0].columns):
                    meilleur = (df, enc, sep)
        except Exception:
            pass
    if meilleur is not None:
        return meilleur
    raise ValueError(f"Impossible de lire le fichier : {path}")

def read_table(path):
    """Lit le fichier CSV avec détection automatique des encodages et séparateurs (Votre fonction)."""
    df, _enc, _sep = read_table_with_format(path)
    return df

def extraire_nom_entreprise(url):
    """Extrait le nom de l'entreprise depuis l'URL LinkedIn."""
    if not isinstance(url, str):
        return None
    url = url.strip().rstrip('/')
    match = re.search(r'/(?:company|school)/([^/?#]+)', url)
    if match:
        return match.group(1).replace('-', ' ').title()
    return None

def normaliser(texte):
    """Met un texte en minuscule, sans accents ni ponctuation, pour comparaison robuste."""
    if not texte:
        return ""
    texte = str(texte).lower()
    texte = unicodedata.normalize('NFKD', texte).encode('ascii', 'ignore').decode('ascii')
    texte = re.sub(r'[^a-z0-9\s]', ' ', texte)
    texte = re.sub(r'\s+', ' ', texte).strip()
    return texte

def nettoyer_titre_brut(title):
    """
    Nettoie un titre de résultat de recherche pour ne garder que la partie utile
    (avant toute mention de "LinkedIn"), et retire les résidus de tiret/pipe en fin
    de chaîne.

    Corrige un problème observé : DuckDuckGo renvoie parfois un titre "pollué" qui
    concatène plusieurs résultats à la suite, ex. :
      "Anthony BOISNARD - Adjoint de Direction ... - LinkedInEmma PECOUT - Spa Manager ..."
    Le mot "LinkedIn" (avec ou sans espace/pipe avant) marque quasi toujours la fin du
    titre utile et le début soit du suffixe de site, soit de la pollution suivante.
    On coupe donc à la première occurrence de "linkedin", peu importe ce qui la précède.
    """
    if not title:
        return ""
    t = str(title)
    idx = t.lower().find("linkedin")
    if idx != -1:
        t = t[:idx]
    t = re.sub(r'[\s\-|]+$', '', t).strip()
    return t

def _segments_titre(title):
    """
    Découpe un titre nettoyé en segments "Nom - Poste - Entreprise".
    IMPORTANT : on ne coupe que sur un tiret ENTOURÉ D'ESPACES (" - "), jamais sur un
    tiret collé à des lettres. Sinon des mots/noms composés comme "Haut-Bugey" ou
    "Sous-directeur" étaient cassés en deux morceaux à tort.
    """
    t = nettoyer_titre_brut(title)
    if not t:
        return []
    return [p.strip() for p in re.split(r'\s+-\s+', t) if p.strip()]

def clean_profile_title(title):
    """Nettoie le titre pour isoler au mieux le Prénom Nom."""
    parts = _segments_titre(title)
    return parts[0] if parts else "Inconnu"

def extraire_intitule_reel(title):
    """
    Extrait l'intitulé de poste RÉEL de la personne à partir du titre du résultat
    de recherche (et non le poste recherché par la requête).

    Format typique LinkedIn :
      "Prénom Nom - Intitulé de poste - Entreprise"
    Le 2e segment correspond à l'intitulé réel du poste.
    """
    parts = _segments_titre(title)
    return parts[1] if len(parts) >= 2 else ""

def extraire_entreprise_reelle(title):
    """
    Extrait le nom de l'ENTREPRISE réelle où travaille la personne, à partir du titre
    du résultat de recherche (3e segment, quand présent) :
      "Prénom Nom - Intitulé de poste - Entreprise"
    """
    parts = _segments_titre(title)
    if len(parts) >= 3:
        return ' - '.join(parts[2:]).strip()
    return ""

def extraire_titre_complet(title):
    """
    Renvoie le titre COMPLET et brut du résultat de recherche pour la personne
    (nettoyé de la mention "LinkedIn" et de tout ce qui suit), sans le découper en segments.
    """
    return nettoyer_titre_brut(title)

def est_poste_dirigeant(intitule_reel):
    """
    Indique si l'intitulé de poste RÉEL de la personne correspond à un poste de dirigeant
    ou de haute responsabilité (directeur général, gérant, président, fondateur, etc.),
    quel que soit le poste qui a permis de la trouver au départ.
    """
    if not intitule_reel:
        return False
    t = normaliser(intitule_reel)
    return any(mot in t for mot in MOTS_CLES_DIRIGEANT)

def est_poste_equipements_bien_etre(intitule_reel):
    """
    Indique si l'intitulé de poste RÉEL de la personne est lié aux équipements bien-être
    (spa, thalasso, wellness, balnéo, thermes...), quel que soit le poste qui a permis de
    la trouver au départ.
    """
    if not intitule_reel:
        return False
    t = normaliser(intitule_reel)
    return any(mot in t for mot in MOTS_CLES_EQUIPEMENTS_BIEN_ETRE)

def extraire_periode_emploi(body):
    """
    Cherche dans la description (body) du résultat de recherche une période d'emploi
    au format LinkedIn, ex: "sept. 2025 - aujourd'hui · 1 an 1 mois".
    Renvoie la période brute trouvée, ou une chaîne vide si le snippet n'en contient pas
    (ce n'est pas systématique : dépend de ce que DuckDuckGo a indexé).
    """
    if not body:
        return ""
    match = PATTERN_PERIODE.search(body)
    if match:
        return match.group(0).strip()
    return ""

def entreprise_correspond(entreprise_cible, entreprise_extraite):
    """Vérifie si l'entreprise extraite correspond à l'entreprise recherchée (comparaison souple)."""
    if not entreprise_extraite:
        return False
    cible_norm = normaliser(entreprise_cible)
    extraite_norm = normaliser(entreprise_extraite)
    if not cible_norm or not extraite_norm:
        return False
    if cible_norm in extraite_norm or extraite_norm in cible_norm:
        return True

    mots_cible = {m for m in cible_norm.split() if len(m) >= 4 and m not in MOTS_GENERIQUES_ENTREPRISE}
    mots_extraite = {m for m in extraite_norm.split() if len(m) >= 4 and m not in MOTS_GENERIQUES_ENTREPRISE}
    if not mots_cible or not mots_extraite:
        return False

    # On exige que TOUS les mots significatifs du plus petit ensemble se retrouvent dans
    # l'autre (et pas un simple mot en commun, trop permissif : "univers" seul ne doit pas
    # suffire à faire correspondre "Univers Wellness" et "L'Univers Informatique", deux
    # entreprises différentes).
    plus_petit, plus_grand = (
        (mots_cible, mots_extraite) if len(mots_cible) <= len(mots_extraite) else (mots_extraite, mots_cible)
    )
    return plus_petit.issubset(plus_grand)

def entreprise_dans_body(body, nom_entreprise):
    """Vérifie si le nom de l'entreprise recherchée apparaît dans la description du résultat."""
    if not body:
        return False
    body_norm = normaliser(body)
    cible_norm = normaliser(nom_entreprise)
    if not cible_norm:
        return False
    mots_cible = {m for m in cible_norm.split() if len(m) >= 4}
    if not mots_cible:
        return cible_norm in body_norm
    return any(m in body_norm for m in mots_cible)

def verifier_emploi_actuel(nom_entreprise, entreprise_reelle, body, periode=""):
    """
    Estime si la personne travaille ENCORE aujourd'hui dans l'entreprise recherchée.
    Se base sur les données publiques indexées (titre du résultat de recherche) :
    ce n'est PAS une vérification en temps réel du profil LinkedIn (nécessiterait
    une connexion authentifiée), mais une estimation à partir de ce qui est indexé.

    Preuve PRINCIPALE : le TITRE du profil confirme l'entreprise (3e segment
    "Nom - Poste - Entreprise"). C'est la preuve la plus fiable.

    Preuve SECONDAIRE (si le titre n'a pas ce 3e segment, ex: LinkedIn/DuckDuckGo
    indexe parfois juste "Nom - Poste" sans la société) : on accepte quand même SI
    ET SEULEMENT SI les DEUX conditions suivantes sont réunies dans l'extrait
    (body) : l'entreprise y est mentionnée ET une période explicitement en cours
    ("aujourd'hui"/"present") y est détectée. Exiger les deux à la fois évite de
    retomber sur le problème initial (la requête contenant déjà le nom de
    l'entreprise, une simple mention seule ne prouve rien).

    Retourne (bool_emploi_actuel, raison_texte).
    """
    periode_norm = normaliser(periode) if periode else ""
    texte_combine_norm = normaliser(f"{entreprise_reelle} {body}")

    titre_confirme = bool(entreprise_reelle) and entreprise_correspond(nom_entreprise, entreprise_reelle)

    if not titre_confirme:
        # Preuve secondaire : entreprise ET période en cours toutes deux détectées
        # dans l'extrait, en l'absence du 3e segment habituel dans le titre.
        periode_en_cours = bool(periode_norm) and any(mot in periode_norm for mot in ["aujourd", "present"])
        if periode_en_cours and entreprise_dans_body(body, nom_entreprise):
            if any(mot in texte_combine_norm for mot in INDICES_ANCIEN_POSTE):
                return False, "Non - indice d'ancien poste detecte (preuve secondaire)"
            return True, f"Oui (preuve secondaire) - entreprise mentionnee + periode en cours dans l'extrait : {periode}"
        if entreprise_reelle:
            return False, f"Non - entreprise differente indiquee dans le titre : {entreprise_reelle}"
        return False, "Non confirme - entreprise non indiquee dans le titre du profil (et pas de preuve secondaire suffisante)"

    # 2. Période détectée avec une date de fin explicite (pas "aujourd'hui"/"present")
    #    = signal fort que le poste est terminé.
    if periode_norm and not any(mot in periode_norm for mot in ["aujourd", "present"]):
        return False, f"Non - periode terminee detectee : {periode}"

    # 3. Indices textuels d'ancien poste
    if any(mot in texte_combine_norm for mot in INDICES_ANCIEN_POSTE):
        return False, "Non - indice d'ancien poste detecte"

    # 4. Entreprise confirmée par le titre, éventuellement renforcée par une période en cours
    if periode_norm and any(mot in periode_norm for mot in ["aujourd", "present"]):
        return True, f"Oui - entreprise et periode en cours confirmees dans le titre : {periode}"

    return True, "Oui - entreprise confirmee dans le titre du profil"

def detecter_colonne(df, mots_cles):
    """Cherche, parmi les colonnes du DataFrame, une colonne dont le nom (normalisé)
    contient l'un des mots-clés donnés (comparaison sur mots entiers, pour éviter par
    exemple qu'un mot-clé "tel" corresponde à tort à une colonne nommée "Hotel")."""
    for col in df.columns:
        col_norm = normaliser(col)
        for mot in mots_cles:
            if re.search(rf'\b{re.escape(mot)}\b', col_norm):
                return col
    return None

def domaine_valide_pour_site(url):
    """Vérifie que l'URL ne pointe pas vers un annuaire/réseau social (donc probablement
    le site officiel de l'entreprise)."""
    try:
        domaine = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not domaine:
        return False
    domaine = domaine[4:] if domaine.startswith("www.") else domaine
    return not any(exclu in domaine for exclu in DOMAINES_EXCLUS_SITE)

def extraire_site_depuis_texte_linkedin(texte):
    """Cherche un motif 'Website: <url>' tel qu'affiché dans la section "About us"
    des pages entreprise LinkedIn, au sein d'un extrait de résultat de recherche."""
    m = re.search(r'website\s*[:\-]?\s*(https?://[^\s|<>")]+)', texte, re.IGNORECASE)
    if m:
        return m.group(1).rstrip('.,;)')
    return ""

def rechercher_site_web_depuis_page_linkedin(url_linkedin, max_retries=2):
    """
    Cherche le site web officiel directement depuis le champ "Website" affiché sur
    la page LinkedIn de l'entreprise elle-même (visible dans l'extrait indexé par le
    moteur de recherche pour cette URL précise) — plus fiable qu'une recherche
    générique par nom, puisque l'info vient de LinkedIn.

    Renvoie (site_web, echec_reseau) — echec_reseau est True uniquement si TOUTES
    les tentatives ont échoué sans la moindre réponse du moteur de recherche
    (signe probable d'un blocage réseau, pas juste "site non trouvé").
    """
    if not url_linkedin:
        return "", False
    query = f'"{url_linkedin}"'
    for attempt in range(1, max_retries + 1):
        try:
            for item in ddgs_text_avec_timeout(query, region="fr-fr", max_results=5):
                href = item.get("href", "")
                if "linkedin.com" not in href.lower():
                    continue
                texte = f"{item.get('title', '')} {item.get('body', '')}"
                site = extraire_site_depuis_texte_linkedin(texte)
                if site:
                    return site, False
            return "", False
        except Exception as e:
            print(f"    ⚠️ Erreur DDG page LinkedIn (tentative {attempt}/{max_retries}) : {e}")
            time.sleep(attempt * random.uniform(2.0, 4.0))
    return "", True

def rechercher_site_web(nom_entreprise, url_linkedin="", max_retries=2):
    """
    Cherche le site officiel de l'entreprise : en priorité depuis le champ "Website"
    de sa propre page LinkedIn, puis par recherche générique par nom en repli si
    la page LinkedIn n'a rien donné. Renvoie (site_web, echec_reseau).
    """
    site, echec = rechercher_site_web_depuis_page_linkedin(url_linkedin, max_retries=max_retries)
    if site:
        return site, False

    # Repli : recherche générique par nom d'entreprise
    query = f"{nom_entreprise} site officiel"
    for attempt in range(1, max_retries + 1):
        try:
            for item in ddgs_text_avec_timeout(query, region="fr-fr", max_results=5):
                url = item.get("href", "")
                if domaine_valide_pour_site(url):
                    return url, False
            return "", False
        except Exception as e:
            print(f"    ⚠️ Erreur DDG site web (tentative {attempt}/{max_retries}) : {e}")
            time.sleep(attempt * random.uniform(2.0, 4.0))
    return "", True

def rechercher_adresse_telephone(nom_entreprise, max_retries=2):
    """Cherche l'adresse et le téléphone de l'entreprise dans les extraits de recherche
    (utilisé seulement pour ce qui est absent du fichier). Best-effort : dépend de ce
    que DuckDuckGo a indexé, à vérifier manuellement en cas de doute.
    Renvoie (adresse, telephone, echec_reseau)."""
    query = f"{nom_entreprise} adresse telephone"
    adresse, telephone = "", ""
    for attempt in range(1, max_retries + 1):
        try:
            for item in ddgs_text_avec_timeout(query, region="fr-fr", max_results=5):
                texte = f"{item.get('title', '')} {item.get('body', '')}"
                if not telephone:
                    m = PHONE_REGEX.search(texte)
                    if m:
                        telephone = m.group(0).strip()
                if not adresse:
                    m2 = ADRESSE_PATTERN.search(texte)
                    if m2:
                        adresse = re.sub(r'\s+', ' ', m2.group(0)).strip()
                if adresse and telephone:
                    break
            return adresse, telephone, False
        except Exception as e:
            print(f"    ⚠️ Erreur DDG adresse/tel (tentative {attempt}/{max_retries}) : {e}")
            time.sleep(attempt * random.uniform(2.0, 4.0))
    return adresse, telephone, True

# Coupe-circuit : si DuckDuckGo bloque systématiquement les recherches secondaires
# (site web / adresse / téléphone) sur plusieurs entreprises d'affilée, on les
# désactive pour le reste du lot, afin de ne pas perdre de temps dessus. La
# recherche de profils LinkedIn (l'essentiel du script) n'est jamais désactivée.
ETAT_RECHERCHES_SECONDAIRES = {"actives": True, "echecs_consecutifs": 0}
SEUIL_DESACTIVATION = 2

def obtenir_infos_entreprise(nom_entreprise, url_linkedin="", site_existant="", adresse_existante="", telephone_existant=""):
    """
    Renvoie (site_web, adresse, telephone) pour l'entreprise : reprend telles quelles
    les valeurs déjà présentes dans le fichier d'entrée, et ne lance une recherche
    QUE pour les informations manquantes — sauf si le coupe-circuit a désactivé les
    recherches secondaires suite à des échecs réseau répétés.
    """
    site_web = (site_existant or "").strip()
    adresse = (adresse_existante or "").strip()
    telephone = (telephone_existant or "").strip()

    if (site_web and adresse and telephone) or not ETAT_RECHERCHES_SECONDAIRES["actives"]:
        return site_web, adresse, telephone

    echecs = []

    if not site_web:
        time.sleep(random.uniform(3.0, 6.0))
        site_web, echec_site = rechercher_site_web(nom_entreprise, url_linkedin)
        echecs.append(echec_site)

    if not adresse or not telephone:
        time.sleep(random.uniform(3.0, 6.0))
        adresse_trouvee, telephone_trouve, echec_contact = rechercher_adresse_telephone(nom_entreprise)
        if not adresse:
            adresse = adresse_trouvee
        if not telephone:
            telephone = telephone_trouve
        echecs.append(echec_contact)

    if echecs and all(echecs):
        ETAT_RECHERCHES_SECONDAIRES["echecs_consecutifs"] += 1
        if ETAT_RECHERCHES_SECONDAIRES["echecs_consecutifs"] >= SEUIL_DESACTIVATION:
            ETAT_RECHERCHES_SECONDAIRES["actives"] = False
            print("⚠️ DuckDuckGo semble bloquer les recherches (site web/adresse/téléphone) "
                  "depuis cette machine : ces recherches sont désactivées pour le reste du lot "
                  "afin de ne pas perdre de temps. La recherche de profils LinkedIn continue normalement.")
    elif echecs:
        ETAT_RECHERCHES_SECONDAIRES["echecs_consecutifs"] = 0

    return site_web, adresse, telephone

def marquer_traite(colonne_url, url_traitee, statut, enc, sep):
    """
    Marque une ligne du fichier D'ENTREE (liste_urls.csv) comme traitée, dans une
    colonne "Traite", et réécrit immédiatement le fichier (dans son format d'origine).
    Permet une reprise fiable même si le job GitHub Actions s'arrête entre deux lots.
    """
    with WRITE_LOCK:
        try:
            df_actuel = pd.read_csv(FICHIER_ENTREE, encoding=enc, sep=sep, dtype=str, keep_default_na=False)
        except Exception:
            df_actuel = read_table(FICHIER_ENTREE)

        if "Traite" not in df_actuel.columns:
            df_actuel["Traite"] = ""

        masque = df_actuel[colonne_url].astype(str).str.strip() == url_traitee.strip()
        df_actuel.loc[masque, "Traite"] = statut
        df_actuel.to_csv(FICHIER_ENTREE, index=False, sep=sep, encoding=enc)

def search_duckduckgo_direct(nom_entreprise, poste, max_results=5, max_retries=3):
    """
    Effectue une recherche directe (Méthode de votre exemple).
    Pas de X-Ray strict, on demande les mots clés naturellement.
    """
    query = f"{nom_entreprise} {poste} linkedin"

    for attempt in range(1, max_retries + 1):
        try:
            results = []
            # Utilisation de la méthode de texte directe comme dans votre exemple
            ddg_generator = ddgs_text_avec_timeout(query, region="fr-fr", max_results=max_results)
            for item in ddg_generator:
                url = item.get("href", "")
                # Filtre corrigé : accepte tous les sous-domaines (fr., www., ca., etc.)
                # et cible spécifiquement les profils personnels (/in/), pas les pages entreprise
                if "linkedin.com/in/" in url:
                    results.append({
                        "title": item.get("title", ""),
                        "url": url,
                        "body": item.get("body", "")
                    })
            return results
        except Exception as e:
            print(f"    ⚠️ Erreur DDG (tentative {attempt}/{max_retries}) : {e}")
            time.sleep(attempt * random.uniform(2.0, 4.0))
    return []

# ==========================================
# SCRIPT PRINCIPAL
# ==========================================

def main():
    if not os.path.exists(FICHIER_ENTREE):
        print(f"❌ Erreur : Le fichier d'entrée '{FICHIER_ENTREE}' est introuvable.")
        sys.exit(1)

    # 1. Chargement et normalisation des données sources
    df_entree, enc_entree, sep_entree = read_table_with_format(FICHIER_ENTREE)
    colonne_url = None
    for col in df_entree.columns:
        if df_entree[col].astype(str).str.contains("linkedin.com", na=False).any():
            colonne_url = col
            break

    if not colonne_url:
        colonne_url = df_entree.columns[0]

    if "Traite" not in df_entree.columns:
        df_entree["Traite"] = ""
        df_entree.to_csv(FICHIER_ENTREE, index=False, sep=sep_entree, encoding=enc_entree)

    # Détection des colonnes déjà existantes (nom / site web / adresse / téléphone) dans
    # le fichier d'entrée, pour ne rechercher que ce qui manque. La colonne URL est
    # exclue de la détection du nom pour ne pas la confondre avec la colonne nom si
    # son en-tête contient aussi le mot "entreprise".
    colonnes_hors_url = [c for c in df_entree.columns if c != colonne_url]
    col_nom = detecter_colonne(df_entree[colonnes_hors_url], MOTS_CLES_COL_NOM) if colonnes_hors_url else None
    col_site = detecter_colonne(df_entree, MOTS_CLES_COL_SITE)
    col_adresse = detecter_colonne(df_entree, MOTS_CLES_COL_ADRESSE)
    col_telephone = detecter_colonne(df_entree, MOTS_CLES_COL_TELEPHONE)
    print(f"ℹ️ Colonnes détectées dans le fichier d'entrée — Nom: {col_nom or 'aucune'}, "
          f"Site: {col_site or 'aucune'}, Adresse: {col_adresse or 'aucune'}, "
          f"Téléphone: {col_telephone or 'aucune'}")

    # Table de correspondance URL -> infos déjà connues (site/adresse/téléphone),
    # construite une seule fois pour éviter de reparcourir le DataFrame à chaque ligne.
    infos_existantes = {}
    for _, ligne in df_entree.iterrows():
        u = str(ligne.get(colonne_url, "")).strip()
        if u and u not in infos_existantes:
            infos_existantes[u] = {
                "nom": str(ligne[col_nom]).strip() if col_nom and pd.notna(ligne[col_nom]) else "",
                "site": str(ligne[col_site]).strip() if col_site and pd.notna(ligne[col_site]) else "",
                "adresse": str(ligne[col_adresse]).strip() if col_adresse and pd.notna(ligne[col_adresse]) else "",
                "telephone": str(ligne[col_telephone]).strip() if col_telephone and pd.notna(ligne[col_telephone]) else "",
            }

    urls_a_traiter = df_entree[colonne_url].dropna().unique().tolist()
    print(f"🚀 {len(urls_a_traiter)} URL(s) détectée(s) dans '{FICHIER_ENTREE}'.")

    # 2. Gestion de la reprise après plantage : on croise 2 sources d'information
    #    a) la colonne "Traite" du fichier d'entrée liste_urls.csv
    #    b) le fichier de sortie déjà généré
    urls_deja_marquees = set(
        df_entree.loc[df_entree["Traite"].astype(str).str.strip() != "", colonne_url]
        .dropna().unique().tolist()
    )

    urls_deja_traitees = set()
    if os.path.exists(FICHIER_SORTIE):
        try:
            df_existant = read_table(FICHIER_SORTIE)
            if "URL Entreprise" in df_existant.columns:
                urls_deja_traitees = set(df_existant["URL Entreprise"].dropna().unique().tolist())
        except Exception:
            pass

    urls_deja_traitees |= urls_deja_marquees
    print(f"ℹ️ Reprise active : {len(urls_deja_traitees)} URL(s) déjà traitée(s) ignorée(s).")

    # 2bis. On ne garde que les URLs restant à traiter
    urls_restantes = [u for u in urls_a_traiter if u.strip() not in urls_deja_traitees]

    if BATCH_SIZE > 0:
        urls_du_lot = urls_restantes[:BATCH_SIZE]
        print(f"📦 Mode lot activé (BATCH_SIZE={BATCH_SIZE}) : {len(urls_du_lot)} URL(s) traitée(s) sur {len(urls_restantes)} restante(s).")
    else:
        urls_du_lot = urls_restantes

    # 3. Traitement séquentiel (1 à 1)
    for index, url in enumerate(urls_du_lot, 1):
        url_clean = url.strip()

        infos_connues = infos_existantes.get(url_clean, {})
        nom_entreprise = infos_connues.get("nom") or extraire_nom_entreprise(url_clean)
        if not nom_entreprise:
            print(f"[{index}/{len(urls_du_lot)}] URL non valide : {url_clean}")
            marquer_traite(colonne_url, url_clean, "Invalide", enc_entree, sep_entree)
            continue

        print(f"[{index}/{len(urls_du_lot)}] Recherche directe pour : {nom_entreprise}...")

        # Site web / adresse / téléphone : recherche désactivée pour accélérer le
        # traitement. On garde uniquement ce qui est déjà présent dans le fichier
        # d'entrée (aucun appel réseau supplémentaire ici).
        site_web = infos_connues.get("site", "")
        adresse = infos_connues.get("adresse", "")
        telephone = infos_connues.get("telephone", "")

        profils_trouves = []
        profils_ecartes = 0
        candidats_ecartes_debug = []
        urls_uniques_profils = set()

        # Itération sur chaque poste
        for poste in POSTES_CIBLES:
            # Temporisation pour ne pas surcharger DuckDuckGo (comme dans votre exemple)
            time.sleep(random.uniform(3.0, 6.0))

            resultats_recherche = search_duckduckgo_direct(nom_entreprise, poste)

            for res in resultats_recherche:
                profil_url = res["url"]
                if profil_url in urls_uniques_profils:
                    continue
                urls_uniques_profils.add(profil_url)

                nom_prenom = clean_profile_title(res["title"])
                intitule_reel = extraire_intitule_reel(res["title"])
                titre_complet = extraire_titre_complet(res["title"])
                entreprise_reelle = extraire_entreprise_reelle(res["title"])
                periode = extraire_periode_emploi(res.get("body", ""))
                niveau_poste = "Dirigeant / Haute responsabilite" if est_poste_dirigeant(intitule_reel) else "Autre"
                domaine_equipements = "Spa / Thalasso / Wellness" if est_poste_equipements_bien_etre(intitule_reel) else ""
                emploi_actuel, raison = verifier_emploi_actuel(
                    nom_entreprise, entreprise_reelle, res.get("body", ""), periode
                )

                # On ne garde QUE les profils dont on estime qu'ils travaillent
                # ENCORE aujourd'hui dans l'entreprise recherchée.
                if not emploi_actuel:
                    profils_ecartes += 1
                    candidats_ecartes_debug.append({
                        "URL Entreprise": url_clean,
                        "Nom Entreprise": nom_entreprise,
                        "Poste Recherche": poste,
                        "Nom Profil": nom_prenom,
                        "Titre Complet": titre_complet,
                        "Entreprise Extraite du Titre": entreprise_reelle,
                        "Periode Detectee": periode,
                        "Lien LinkedIn": profil_url,
                        "Raison Rejet": raison,
                    })
                    continue

                profils_trouves.append(
                    (nom_prenom, profil_url, poste, intitule_reel, titre_complet,
                     entreprise_reelle, periode, niveau_poste, domaine_equipements, raison)
                )

        # Journal de diagnostic : permet de comprendre exactement pourquoi un profil
        # connu (dont on sait qu'il travaille bien dans l'entreprise) a été écarté,
        # au lieu de se fier uniquement au compteur "profils écartés".
        if candidats_ecartes_debug:
            df_debug = pd.DataFrame(candidats_ecartes_debug)
            with WRITE_LOCK:
                entete_debug = not os.path.exists(FICHIER_DEBUG_ECARTES)
                df_debug.to_csv(FICHIER_DEBUG_ECARTES, mode="a", header=entete_debug,
                                 index=False, sep=";", encoding="utf-8-sig")

        # 4. Préparation de la ligne finale
        row_data = {
            "URL Entreprise": url_clean,
            "Nom Entreprise": nom_entreprise,
            "Site Web": site_web,
            "Adresse": adresse,
            "Telephone": telephone,
            "Statut Traitement": "Traite - Profil Trouve" if profils_trouves else "Traite - Aucun profil trouve"
        }

        # Alignement en colonnes (Collaborateur 1, Collaborateur 2...)
        for idx, (nom_prenom, p_url, poste, intitule_reel, titre_complet, entreprise_reelle, periode, niveau_poste, domaine_equipements, raison) in enumerate(profils_trouves, 1):
            row_data[f"Poste Recherche {idx}"] = poste
            row_data[f"Intitule Reel {idx}"] = intitule_reel
            row_data[f"Titre Complet {idx}"] = titre_complet
            row_data[f"Entreprise Actuelle {idx}"] = entreprise_reelle or nom_entreprise
            row_data[f"Periode Detectee {idx}"] = periode
            row_data[f"Niveau Poste {idx}"] = niveau_poste
            row_data[f"Domaine Equipements {idx}"] = domaine_equipements
            row_data[f"Collaborateur {idx}"] = nom_prenom
            row_data[f"Lien LinkedIn {idx}"] = p_url
            row_data[f"Verification Emploi {idx}"] = raison

        df_nouvelle_ligne = pd.DataFrame([row_data])

        # 5. Écriture thread-safe en temps réel
        with WRITE_LOCK:
            if os.path.exists(FICHIER_SORTIE):
                try:
                    df_existant = read_table(FICHIER_SORTIE)
                    df_final = pd.concat([df_existant, df_nouvelle_ligne], ignore_index=True)
                except Exception:
                    df_final = df_nouvelle_ligne
            else:
                df_final = df_nouvelle_ligne

            df_final.to_csv(FICHIER_SORTIE, index=False, sep=";", encoding="utf-8-sig")

        # Marque la ligne comme traitée dans liste_urls.csv (reprise fiable entre lots)
        marquer_traite(colonne_url, url_clean, "Oui", enc_entree, sep_entree)

        print(f"  ✅ {len(profils_trouves)} profil(s) retenu(s) (emploi actuel confirmé) "
              f"— {profils_ecartes} écarté(s) (entreprise différente ou non confirmée) pour {nom_entreprise}.")
        print(f"     🌐 Site: {site_web or 'non trouvé'} | 📍 Adresse: {adresse or 'non trouvée'} | "
              f"📞 Tél: {telephone or 'non trouvé'}")

    restant_apres_lot = len(urls_restantes) - len(urls_du_lot)
    print(f"\n🎉 Script terminé pour ce lot. Fichier mis à jour : '{FICHIER_SORTIE}'")
    print(f"📊 Il reste {restant_apres_lot} URL(s) à traiter.")

    # Code de sortie utilisé par le workflow GitHub Actions pour savoir s'il doit
    # relancer un lot suivant : 2 = il reste des URLs, 0 = tout est traité.
    sys.exit(2 if restant_apres_lot > 0 else 0)

if __name__ == "__main__":
    main()