import csv
import os
import re
import socket
import smtplib
import time
import random
import unicodedata
import requests
import dns.resolver
from urllib.parse import urlparse, urljoin, unquote

# Fichiers de configuration
INPUT_FILE = "liste.csv"
OUTPUT_FILE = "leads_enrichis.csv"

# Nombre max de leads traités en une seule exécution (0 ou vide = pas de limite).
# Utile en CI (GitHub Actions) pour traiter par petits lots successifs et éviter
# de se faire bloquer par les serveurs mail à force de sonder trop de domaines
# d'affilée depuis la même IP. Le script sauvegarde sa progression ligne par ligne
# (colonne Status = "Traité"), donc une reprise ultérieure est toujours sûre.
MAX_LEADS_PAR_RUN = int(os.environ.get("MAX_LEADS_PAR_RUN", "0") or "0")

# Domaines à ignorer quand on cherche le SITE OFFICIEL d'une entreprise (annuaires,
# réseaux sociaux, plateformes d'avis... ce ne sont jamais le site officiel).
DOMAINES_EXCLUS_SITE_OFFICIEL = {
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "wikipedia.org", "pagesjaunes.fr", "societe.com", "google.com", "google.fr",
    "tripadvisor.com", "tripadvisor.fr", "indeed.com", "glassdoor.fr", "glassdoor.com",
    "viadeo.com", "youtube.com", "pinterest.com", "yelp.com", "yelp.fr",
}


def corriger_mojibake(text):
    """Répare les accents cassés (ex: 'AnaÃ¯s' -> 'Anaïs') qui apparaissent quand
    un texte réellement encodé en UTF-8 a été lu par erreur avec l'encodage cp1252."""
    if not text or ("Ã" not in text and "â€" not in text):
        return text
    try:
        reparé = text.encode('cp1252').decode('utf-8')
        return reparé
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def clean_string(text):
    """Nettoie le texte : minuscule, supprime les espaces et les accents."""
    if not text:
        return ""
    text = text.strip().lower()
    text = "".join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]', '', text)


def normaliser_libelle(texte):
    """Comme clean_string, mais GARDE les espaces (utile pour comparer des libellés
    de colonnes mot à mot, ex: reconnaître "nom" comme mot entier dans "Nom Entreprise"
    sans le confondre avec un mot qui contiendrait "nom" par hasard)."""
    if not texte:
        return ""
    texte = str(texte).strip().lower()
    texte = "".join(c for c in unicodedata.normalize('NFD', texte) if unicodedata.category(c) != 'Mn')
    texte = re.sub(r'[^a-z0-9\s]', ' ', texte)
    return re.sub(r'\s+', ' ', texte).strip()


def formater_url(url):
    """Nettoie et formate correctement l'URL pour éviter l'erreur 'No host supplied'."""
    url = url.strip()
    if not url:
        return ""
    url = re.sub(r'^(https?://)?(www\.)?', '', url)
    return "https://www." + url


def extraire_identité_depuis_url(url):
    """Extrait proprement le prénom et le nom à partir du slug de l'URL LinkedIn."""
    match = re.search(r'/in/([^/?]+)', url)
    if not match:
        return "Inconnu", "Inconnu"

    slug = match.group(1)
    slug_clean = re.sub(r'[-_]?[0-9a-fA-F]{6,20}$', '', slug)
    parts = [p for p in re.split(r'[-_]', slug_clean) if p]

    if len(parts) >= 2:
        prenom = parts[0].capitalize()
        nom = " ".join([p.capitalize() for p in parts[1:]])
    else:
        prenom = slug_clean.capitalize()
        nom = "Inconnu"

    return prenom, nom


def extraire_entreprise_depuis_url(url):
    """Extrait le nom de la société à partir du slug de l'URL LinkedIn entreprise
    (ex: https://www.linkedin.com/company/ma-super-societe -> "Ma Super Societe")."""
    if not url:
        return None

    url = formater_url(url)
    match = re.search(r'/company/([^/?]+)', url)
    if not match:
        return None

    slug = match.group(1)
    # LinkedIn ajoute parfois un identifiant numérique en fin de slug, on le retire
    slug_clean = re.sub(r'[-_]?[0-9]{4,}$', '', slug)
    mots = [m for m in re.split(r'[-_]', slug_clean) if m]

    if not mots:
        return None

    return " ".join(m.capitalize() for m in mots)


def deviner_domaine_depuis_nom_entreprise(nom_entreprise):
    """Construit une hypothèse de domaine à partir du nom de société (sans espaces/accents).
    C'est une estimation : la vérification SMTP sert justement à la confirmer ou l'infirmer.
    Utilisé seulement en DERNIER recours, quand la recherche du vrai site web n'a rien donné."""
    domaine = clean_string(nom_entreprise) + ".com"
    return domaine


def extraire_domaine_depuis_site_web(url):
    """Extrait le domaine propre à partir d'une URL de site web fournie directement
    (ex: 'http://www.masociete.fr/contact' -> 'masociete.fr'). Aucune estimation ici :
    c'est le domaine réel donné par l'utilisateur (ou trouvé par une recherche)."""
    if not url:
        return None

    url = url.strip()
    if not url:
        return None

    if not re.match(r'^https?://', url, flags=re.IGNORECASE):
        url = "https://" + url

    try:
        domaine = urlparse(url).netloc
    except ValueError:
        return None

    domaine = domaine.split('@')[-1]          # au cas où un email aurait été collé par erreur
    domaine = domaine.split(':')[0]            # retire un éventuel port
    domaine = re.sub(r'^www\.', '', domaine, flags=re.IGNORECASE)
    domaine = domaine.strip('/').lower()

    return domaine or None


def rechercher_duckduckgo_html(requete, max_resultats=5):
    """Interroge DuckDuckGo (interface HTML publique, sans clé API) et renvoie une
    liste de résultats {"url", "titre", "snippet"} pour la requête donnée.

    ATTENTION - best-effort : ce n'est PAS une API officielle, c'est une lecture de la
    page de résultats HTML. Ça peut cesser de fonctionner si DuckDuckGo change son site,
    et ne doit pas être utilisé de façon intensive."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        reponse = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": requete},
            headers=headers,
            timeout=8,
        )
        if reponse.status_code != 200:
            return []
    except Exception:
        return []

    liens = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        reponse.text, flags=re.IGNORECASE | re.DOTALL
    )
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</a>',
        reponse.text, flags=re.IGNORECASE | re.DOTALL
    )

    resultats = []
    for i, (lien, titre_html) in enumerate(liens[:max_resultats]):
        if "uddg=" in lien:
            match = re.search(r'uddg=([^&]+)', lien)
            if match:
                lien = unquote(match.group(1))
        titre = re.sub(r'<[^<]+?>', '', titre_html).strip()
        snippet = re.sub(r'<[^<]+?>', '', snippets[i]).strip() if i < len(snippets) else ""
        resultats.append({"url": lien, "titre": titre, "snippet": snippet})

    return resultats


def nettoyer_titre_linkedin(title):
    """
    Nettoie un titre de résultat de recherche pour ne garder que la partie utile
    (avant toute mention de "LinkedIn"), et retire les résidus de tiret/pipe en fin
    de chaîne. Le mot "LinkedIn" marque quasi toujours la fin du titre utile.
    """
    if not title:
        return ""
    t = str(title)
    idx = t.lower().find("linkedin")
    if idx != -1:
        t = t[:idx]
    t = re.sub(r'[\s\-|]+$', '', t).strip()
    return t


def extraire_nom_complet_depuis_titre(title):
    """
    Extrait le NOM COMPLET (Prénom Nom) tel qu'affiché dans le titre d'un résultat
    de recherche pour un profil LinkedIn. Format typique : "Prénom Nom - Poste -
    Entreprise | LinkedIn" -> on garde le 1er segment (avant le 1er " - ").
    On ne coupe que sur un tiret ENTOURÉ D'ESPACES, jamais collé à des lettres,
    pour ne pas casser à tort des noms composés (ex: "Jean-Pierre").
    """
    t = nettoyer_titre_linkedin(title)
    if not t:
        return ""
    segments = [p.strip() for p in re.split(r'\s+-\s+', t) if p.strip()]
    return segments[0] if segments else ""


def capitaliser_mot_compose(mot):
    """Capitalise correctement un mot pouvant contenir un tiret (ex: 'jean-pierre'
    -> 'Jean-Pierre', pas 'Jean-pierre' comme le ferait str.capitalize() seul)."""
    return "-".join(p.capitalize() for p in mot.split("-"))


def separer_prenom_nom(nom_complet):
    """Découpe un nom complet 'Prénom Nom(s)' (trouvé via recherche web) en (prenom, nom)."""
    if not nom_complet:
        return None, None
    mots = [m for m in nom_complet.strip().split() if m]
    if len(mots) >= 2:
        return capitaliser_mot_compose(mots[0]), " ".join(capitaliser_mot_compose(m) for m in mots[1:])
    if len(mots) == 1:
        return capitaliser_mot_compose(mots[0]), "Inconnu"
    return None, None


def rechercher_nom_depuis_linkedin(url_profil, max_resultats=5):
    """
    NOUVEAU : cherche le nom EXACT de la personne tel qu'affiché sur son profil
    LinkedIn, via une recherche web ciblée sur cette URL précise - PAS en devinant
    depuis le slug de l'URL (souvent tronqué, mal accentué, ou suivi d'un identifiant
    aléatoire, ex: "jean-dup-4a2b1c" -> mauvaise reconstruction du nom). Le titre du
    résultat de recherche est formaté par LinkedIn comme "Prénom Nom - Poste -
    Entreprise | LinkedIn", dont on extrait le 1er segment.

    Renvoie (prenom, nom) si trouvé, sinon (None, None) - l'appelant doit alors se
    rabattre sur l'extraction depuis l'URL (moins fiable mais toujours disponible).
    """
    if not url_profil:
        return None, None

    resultats = rechercher_duckduckgo_html(f'"{url_profil}"', max_resultats=max_resultats)
    for r in resultats:
        if "linkedin.com/in/" not in r["url"].lower():
            continue
        nom_complet = extraire_nom_complet_depuis_titre(r["titre"])
        if nom_complet:
            prenom, nom = separer_prenom_nom(nom_complet)
            if prenom:
                return prenom, nom
    return None, None


def rechercher_liens_web_pour_domaine(domaine, max_resultats=5):
    """Interroge DuckDuckGo pour trouver des pages sur tout le web (pas seulement le
    site de l'entreprise) mentionnant une adresse e-mail du domaine cible -
    communiqués de presse, annuaires professionnels, actes de conférence, etc."""
    requete = f'"@{domaine}"'
    resultats = rechercher_duckduckgo_html(requete, max_resultats=max_resultats)
    return [r["url"] for r in resultats]


def extraire_site_depuis_texte_linkedin(texte):
    """Cherche un motif 'Website: <url>' tel qu'affiché dans la section "About us"
    des pages entreprise LinkedIn, au sein d'un extrait de résultat de recherche."""
    m = re.search(r'website\s*[:\-]?\s*(https?://[^\s|<>")]+)', texte, re.IGNORECASE)
    if m:
        return m.group(1).rstrip('.,;)')
    return ""


def domaine_valide_pour_site_officiel(url):
    """Vérifie que l'URL ne pointe pas vers un annuaire/réseau social (donc probablement
    le site officiel de l'entreprise)."""
    try:
        domaine = urlparse(url).netloc.lower()
    except Exception:
        return False
    if not domaine:
        return False
    domaine = domaine[4:] if domaine.startswith("www.") else domaine
    return not any(exclu in domaine for exclu in DOMAINES_EXCLUS_SITE_OFFICIEL)


def rechercher_site_web_entreprise(nom_entreprise, url_entreprise_linkedin=""):
    """
    NOUVEAU : cherche le VRAI site web officiel de l'entreprise, dans cet ordre :

    1) En priorité depuis le champ "Website" affiché sur la page LinkedIn de
       l'entreprise elle-même (visible dans l'extrait indexé par DuckDuckGo pour
       cette URL précise) - plus fiable car l'info vient directement de LinkedIn.
    2) En repli, une recherche web générique par nom d'entreprise ("... site officiel"),
       en écartant les résultats qui pointent vers des annuaires/réseaux sociaux.

    Renvoie (domaine, source) si trouvé, sinon (None, None). Le domaine trouvé ici
    remplace ensuite la devinette par défaut (nom + ".com"), et le script reprend
    normalement son traitement habituel (scraping du site, SMTP...) avec ce domaine.
    """
    if url_entreprise_linkedin:
        resultats = rechercher_duckduckgo_html(f'"{url_entreprise_linkedin}"', max_resultats=5)
        for r in resultats:
            if "linkedin.com" not in r["url"].lower():
                continue
            site = extraire_site_depuis_texte_linkedin(f"{r['titre']} {r['snippet']}")
            if site:
                domaine = extraire_domaine_depuis_site_web(site)
                if domaine:
                    return domaine, "page LinkedIn de l'entreprise (champ Website)"

    if nom_entreprise and nom_entreprise != "Entreprise Inconnue":
        time.sleep(random.uniform(1.0, 2.0))
        resultats = rechercher_duckduckgo_html(f"{nom_entreprise} site officiel", max_resultats=5)
        for r in resultats:
            if domaine_valide_pour_site_officiel(r["url"]):
                domaine = extraire_domaine_depuis_site_web(r["url"])
                if domaine:
                    return domaine, "recherche web (site officiel)"

    return None, None


def deviner_entreprise_et_domaine(prenom, nom):
    """Cherche l'entreprise de la personne via une recherche DuckDuckGo (best effort)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    requete = f"linkedin {prenom} {nom}"

    # BUG CORRIGÉ : l'URL précédente concaténait la requête sans l'encoder
    # ni utiliser le bon endpoint/paramètre ("q="), ce qui provoquait une
    # erreur silencieuse (attrapée par le except) à chaque appel.
    url_recherche = "https://api.duckduckgo.com/"
    params = {"q": requete, "format": "json", "no_html": 1, "skip_disambig": 1}

    try:
        response = requests.get(url_recherche, headers=headers, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            texte = (data.get("Heading", "") or "") + " " + (data.get("AbstractText", "") or "")

            match_company = re.search(r'(?:chez|at|manager|director|responsable)\s+([A-Z][a-zA-Z0-9\s]+)', texte)
            if match_company:
                entreprise = match_company.group(1).strip()
                domaine = clean_string(entreprise) + ".com"
                return entreprise, domaine
    except Exception as e:
        print(f"    (info) recherche entreprise échouée : {e}")

    return "Entreprise Inconnue", "entreprise.com"


def generer_patterns_emails(prenom, nom, domaine, pattern_priorite=None):
    """Génère une liste ordonnée (du plus probable au moins probable) de formats
    d'e-mail professionnels courants. Retourne une liste de tuples (label, email).

    Si pattern_priorite est fourni (structure détectée sur le site web de l'entreprise,
    ex: 'prenom.nom', 'initiale.nom', 'prenom_nom'...), ce format est placé en tête de
    liste car il reflète la convention réelle de l'entreprise plutôt qu'une hypothèse
    générique."""
    p = clean_string(prenom)
    # "Inconnu" est notre valeur par défaut interne quand le nom n'a pas pu être extrait
    # (ex: slug LinkedIn sans tiret comme 'helenebrum') - ce n'est PAS un vrai nom de
    # famille et ne doit jamais se retrouver dans un email généré.
    nom_connu = bool(nom) and nom.strip().lower() != "inconnu"
    n = clean_string(nom).replace(" ", "") if nom_connu else ""

    formats_connus = {
        'prenom.nom':        f"{p}.{n}" if p and n else "",
        'prenom_nom':        f"{p}_{n}" if p and n else "",
        'prenom-nom':        f"{p}-{n}" if p and n else "",
        'initiale.nom':      f"{p[0]}.{n}" if p and n else "",
        'initiale_nom':      f"{p[0]}_{n}" if p and n else "",
        'initiale-nom':      f"{p[0]}-{n}" if p and n else "",
        'prenom.initiale':   f"{p}.{n[0]}" if p and n else "",
        'prenom_initiale':   f"{p}_{n[0]}" if p and n else "",
        'prenom-initiale':   f"{p}-{n[0]}" if p and n else "",
    }

    candidats = []

    if pattern_priorite and formats_connus.get(pattern_priorite):
        candidats.append((f"{pattern_priorite} (détecté sur le site)", f"{formats_connus[pattern_priorite]}@{domaine}"))

    if p and n:
        candidats.append(("prenom.nom", f"{p}.{n}@{domaine}"))
        candidats.append(("prenomnom", f"{p}{n}@{domaine}"))
        candidats.append(("p.nom", f"{p[0]}.{n}@{domaine}"))
        candidats.append(("pnom", f"{p[0]}{n}@{domaine}"))
        candidats.append(("nom.prenom", f"{n}.{p}@{domaine}"))
        candidats.append(("prenom_nom", f"{p}_{n}@{domaine}"))
        candidats.append(("nom", f"{n}@{domaine}"))
    if p:
        candidats.append(("prenom", f"{p}@{domaine}"))

    # Supprime les doublons éventuels tout en gardant l'ordre de priorité
    vus = set()
    resultat = []
    for label, email in candidats:
        if email and email not in vus:
            vus.add(email)
            resultat.append((label, email))

    return resultat


def get_mx_record(domaine):
    """Résout le VRAI serveur mail (enregistrement MX) du domaine, avant toute
    tentative de connexion SMTP. AVANT cette correction, le script se connectait
    directement au nom de domaine (ex: adisseo.com:25) - ce qui est souvent le
    serveur du SITE WEB, pas celui qui reçoit les emails (la plupart des
    entreprises utilisent Microsoft 365/Google Workspace pour leurs emails, sur un
    serveur totalement différent). Se connecter au mauvais serveur fait échouer la
    vérification même quand le port 25 n'est pas bloqué et que le domaine n'est pas
    catch-all - ce n'était pas un problème réseau, mais une erreur de méthode.

    Essaie d'abord des résolveurs publics (Google/Cloudflare), puis se replie sur le
    résolveur système par défaut si ceux-ci sont bloqués par le réseau local."""
    tentatives = [
        ("résolveurs publics (8.8.8.8 / 1.1.1.1)", ['8.8.8.8', '1.1.1.1']),
        ("résolveur système par défaut", None),
    ]

    for nom_tentative, nameservers in tentatives:
        try:
            resolver = dns.resolver.Resolver()
            if nameservers:
                resolver.nameservers = nameservers
            resolver.timeout = 5
            resolver.lifetime = 8
            records = resolver.resolve(domaine, 'MX')
            mx = str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip('.')
            return mx, None
        except dns.resolver.NXDOMAIN:
            return None, "Domaine introuvable (NXDOMAIN)"
        except dns.resolver.NoAnswer:
            return None, "Le domaine existe mais n'a aucun enregistrement MX"
        except Exception:
            continue  # on tente la méthode suivante

    return None, "Échec DNS via toutes les méthodes (blocage réseau probable)"


def detecter_catch_all(domaine, mx_server):
    """Teste si le serveur mail du domaine accepte N'IMPORTE QUELLE adresse
    (mode 'catch-all'). Si oui, un résultat 'Valide' via RCPT TO ne prouve rien :
    il faut le signaler plutôt que de laisser croire à une vérification fiable."""
    import uuid
    faux_local_part = f"verif-inexistante-{uuid.uuid4().hex[:10]}"
    resultat = ping_smtp(f"{faux_local_part}@{domaine}", mx_server)
    return resultat == "Valide (SMTP 250)"


def extraire_emails_dune_page(html):
    """Repère les adresses e-mail présentes en clair dans une page (liens mailto:
    ou texte brut), en filtrant les faux positifs courants (fichiers image type
    'photo@2x.png', domaines techniques tiers comme les CDN/trackers, etc.)."""
    if not html:
        return []

    trouve = set()
    for m in re.findall(r'mailto:([^"\'>\s?]+)', html, flags=re.IGNORECASE):
        email = m.split('?')[0].strip()
        if '@' in email:
            trouve.add(email.lower())
    for m in re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', html):
        trouve.add(m.lower())

    extensions_a_exclure = {
        'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'ico', 'css', 'js',
        'woff', 'woff2', 'ttf', 'eot', 'map', 'json', 'xml', 'pdf'
    }
    domaines_a_exclure = {
        'sentry.io', 'wixpress.com', 'wix.com', 'cloudflare.com', 'googleapis.com',
        'gstatic.com', 'schema.org', 'w3.org', 'godaddy.com', 'example.com',
        'domain.com', 'yourdomain.com', 'email.com', 'google.com', 'facebook.com',
        'twitter.com', 'x.com', 'instagram.com', 'linkedin.com', 'youtube.com'
    }

    resultat = []
    for email in trouve:
        local, sep, domaine_email = email.partition('@')
        if not sep or not domaine_email or '.' not in domaine_email:
            continue
        derniere_partie = domaine_email.rsplit('.', 1)[-1]
        if derniere_partie in extensions_a_exclure or domaine_email in domaines_a_exclure:
            continue
        if len(local) > 64 or len(domaine_email) > 253:
            continue
        resultat.append(email)

    return resultat


def detecter_pattern_email(local_part):
    """Analyse la partie avant le '@' d'un email nominatif trouvé sur le site pour en
    déduire la structure utilisée (séparateur + forme initiale/nom complet). Ne classe
    que les formats avec séparateur explicite ('.', '_', '-') : les formats collés sans
    séparateur (ex: 'jdupont') sont trop ambigus pour être déduits de façon fiable sans
    connaître le vrai prénom/nom de la personne derrière cet email."""
    local = local_part.lower()
    for sep in ['.', '_', '-']:
        if sep in local:
            parts = local.split(sep)
            if len(parts) == 2 and all(re.fullmatch(r'[a-z]+', part) for part in parts):
                a, b = parts
                if len(a) == 1 and len(b) > 1:
                    return f'initiale{sep}nom'
                if len(b) == 1 and len(a) > 1:
                    return f'prenom{sep}initiale'
                if len(a) > 1 and len(b) > 1:
                    return f'prenom{sep}nom'
    return None


def extraire_liens_pertinents(html, domaine, mots_cles, limite=6):
    """Analyse les liens <a href=...> d'une page (typiquement le menu de navigation)
    pour trouver ceux qui pointent probablement vers une page contact/équipe/à-propos,
    en se basant sur le texte du lien OU son URL. Plus robuste que de deviner des chemins
    fixes, puisque chaque site a sa propre structure d'URL (ex: '/eu/contact-us' au lieu
    de '/contact'). Ne garde que les liens internes au même domaine."""
    if not html:
        return []

    liens_bruts = re.findall(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html, flags=re.IGNORECASE | re.DOTALL
    )

    base = f"https://{domaine}"
    trouves = []
    for href, texte_html in liens_bruts:
        texte = re.sub(r'<[^<]+?>', '', texte_html).strip().lower()
        href_lower = href.lower()

        if any(mot in href_lower or mot in texte for mot in mots_cles):
            url_absolue = urljoin(base, href)
            if domaine in urlparse(url_absolue).netloc:
                trouves.append(url_absolue)

    # Dédoublonnage en gardant l'ordre d'apparition, puis on limite le nombre de pages
    vus = set()
    resultat = []
    for url in trouves:
        if url not in vus:
            vus.add(url)
            resultat.append(url)
        if len(resultat) >= limite:
            break

    return resultat


def domaines_lies(domaine_original, domaine_trouve):
    """Vérifie que le domaine trouvé sur une page est plausiblement lié au domaine
    d'origine, avant d'accepter de basculer dessus. Règle : le nom principal du
    domaine d'origine (sans TLD, ex: 'bwt' pour 'bwt.com') doit apparaître dans le
    domaine trouvé (ex: 'bwt-group.com' -> OK, 'agence-web-tierce.com' -> refusé).
    Évite d'adopter par erreur un domaine totalement étranger repéré sur la page
    (prestataire, outil tiers, etc.)."""
    if not domaine_original or not domaine_trouve:
        return False
    if domaine_original.lower() == domaine_trouve.lower():
        return True

    nom_principal = domaine_original.split('.')[0].lower()
    if len(nom_principal) <= 2:
        # Nom trop court (ex: 'bw.com') : exige une correspondance exacte du 1er segment
        # pour éviter les faux positifs (2 lettres peuvent apparaître n'importe où).
        return nom_principal == domaine_trouve.split('.')[0].lower()

    return nom_principal in domaine_trouve.lower()


def analyser_site_web_entreprise(domaine):
    """Visite la page d'accueil, puis DÉCOUVRE dynamiquement les liens contact/équipe/
    à-propos présents dans son menu de navigation (plutôt que de deviner des chemins fixes
    qui ne correspondent pas forcément à la structure du site), pour :
    1) confirmer le vrai domaine mail utilisé (parfois différent du site web),
    2) repérer un email générique de contact, et 3) déduire la structure de nommage
    réellement utilisée par l'entreprise à partir d'emails nominatifs trouvés en clair.

    Best-effort : ne fonctionne pas sur les sites qui n'affichent leurs emails que via
    JavaScript dynamique (formulaire de contact sans mailto: en clair dans le HTML brut),
    ni sur les entreprises qui, par choix (souvent RGPD), ne publient aucun email
    individuel et n'utilisent que des formulaires de contact - dans ce cas, aucune
    amélioration du scraping n'y changera rien : c'est structurel, pas un bug."""
    resultat_vide = {'domaine_confirme': None, 'pattern_detecte': None,
                      'email_generique': None, 'exemples_nominatifs': []}
    if not domaine:
        return resultat_vide

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    mots_cles_liens = ['contact', 'team', 'equipe', 'équipe', 'about', 'a-propos',
                        'propos', 'governance', 'gouvernance', 'staff', 'people']

    tous_les_emails = []
    pages_visitees = set()

    # 1) Page d'accueil : toujours visitée en premier, sert aussi à découvrir les liens
    url_accueil = f"https://{domaine}"
    html_accueil = None
    try:
        reponse = requests.get(url_accueil, headers=headers, timeout=6)
        if reponse.status_code == 200:
            html_accueil = reponse.text
            pages_visitees.add(url_accueil)
            tous_les_emails.extend(extraire_emails_dune_page(html_accueil))
    except Exception:
        pass

    # 2) Liens découverts dynamiquement dans la navigation de la page d'accueil
    liens_decouverts = extraire_liens_pertinents(html_accueil, domaine, mots_cles_liens) if html_accueil else []

    # 3) Filet de sécurité : quelques chemins classiques, au cas où rien n'a été découvert
    #    (site sans lien HTML classique détectable, page d'accueil inaccessible, etc.)
    chemins_secours = ["/contact", "/contact-us", "/nous-contacter", "/en/contact",
                        "/equipe", "/team", "/about", "/a-propos"]
    urls_secours = [f"https://{domaine}{chemin}" for chemin in chemins_secours]

    urls_a_visiter = liens_decouverts + [u for u in urls_secours if u not in liens_decouverts]

    for url in urls_a_visiter:
        if url in pages_visitees:
            continue
        pages_visitees.add(url)
        try:
            reponse = requests.get(url, headers=headers, timeout=6)
            if reponse.status_code == 200:
                tous_les_emails.extend(extraire_emails_dune_page(reponse.text))
        except Exception:
            continue
        # Dès qu'on a trouvé au moins un email nominatif (pas juste générique), on peut
        # s'arrêter plus tôt pour limiter le nombre de requêtes vers le site.
        prefixes_generiques_arret = {'contact', 'info', 'hello', 'bonjour', 'support'}
        if any(e.split('@')[0] not in prefixes_generiques_arret for e in tous_les_emails):
            break

    if not tous_les_emails:
        return resultat_vide

    vus = set()
    emails_uniques = []
    for email in tous_les_emails:
        if email not in vus:
            vus.add(email)
            emails_uniques.append(email)

    prefixes_generiques = {
        'contact', 'info', 'hello', 'bonjour', 'commercial', 'office', 'support',
        'sales', 'hr', 'rh', 'recrutement', 'jobs', 'careers', 'presse', 'press',
        'marketing', 'admin', 'webmaster', 'noreply', 'no-reply', 'contactez'
    }

    emails_nominatifs = [e for e in emails_uniques if e.split('@')[0] not in prefixes_generiques]
    emails_generiques = [e for e in emails_uniques if e.split('@')[0] in prefixes_generiques]

    # Domaine le plus fréquent parmi les emails trouvés = domaine mail réel de l'entreprise.
    # Sécurité : on ne considère QUE les domaines dont le nom principal correspond bien
    # au domaine d'origine (ex: 'bwt' dans 'bwt-group.com' est OK, un domaine totalement
    # différent trouvé par erreur sur la page - agence web, prestataire... - est ignoré).
    domaines_comptes = {}
    for e in emails_uniques:
        d = e.split('@')[-1]
        domaines_comptes[d] = domaines_comptes.get(d, 0) + 1
    domaines_comptes_surs = {d: c for d, c in domaines_comptes.items() if domaines_lies(domaine, d)}
    domaine_confirme = max(domaines_comptes_surs, key=domaines_comptes_surs.get) if domaines_comptes_surs else None

    # Structure la plus fréquente parmi les emails nominatifs trouvés
    patterns_trouves = [detecter_pattern_email(e.split('@')[0]) for e in emails_nominatifs]
    patterns_trouves = [p for p in patterns_trouves if p]
    pattern_detecte = None
    if patterns_trouves:
        comptes_patterns = {}
        for p in patterns_trouves:
            comptes_patterns[p] = comptes_patterns.get(p, 0) + 1

        pattern_detecte = max(comptes_patterns, key=comptes_patterns.get)

    return {
        'domaine_confirme': domaine_confirme,
        'pattern_detecte': pattern_detecte,
        'email_generique': emails_generiques[0] if emails_generiques else None,
        'exemples_nominatifs': emails_nominatifs[:5],
    }


def rechercher_email_domaine_sur_web(domaine):
    """Recherche sur le web ENTIER (au-delà du site propre de l'entreprise, déjà
    couvert par analyser_site_web_entreprise) des pages mentionnant une adresse e-mail
    du domaine cible, pour en déduire la structure de nommage réellement utilisée.
    S'arrête dès qu'un résultat exploitable est trouvé - inutile de continuer à
    chercher une fois qu'on a une preuve concrète."""
    resultat_vide = {'pattern_detecte': None, 'email_generique': None, 'exemples_nominatifs': []}
    if not domaine:
        return resultat_vide

    urls = rechercher_liens_web_pour_domaine(domaine)
    if not urls:
        return resultat_vide

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tous_les_emails = []

    for url in urls:
        try:
            reponse = requests.get(url, headers=headers, timeout=6)
            if reponse.status_code == 200:
                emails_page = extraire_emails_dune_page(reponse.text)
                # On ne garde que les emails du domaine qui nous intéresse (le reste
                # de la page peut mentionner d'autres sociétés/domaines sans rapport).
                emails_page = [e for e in emails_page if e.endswith(f"@{domaine}")]
                tous_les_emails.extend(emails_page)
        except Exception:
            continue

        if tous_les_emails:
            break  # on arrête dès qu'on a trouvé quelque chose, comme demandé

    if not tous_les_emails:
        return resultat_vide

    vus = set()
    emails_uniques = []
    for e in tous_les_emails:
        if e not in vus:
            vus.add(e)
            emails_uniques.append(e)

    prefixes_generiques = {
        'contact', 'info', 'hello', 'bonjour', 'commercial', 'office', 'support',
        'sales', 'hr', 'rh', 'recrutement', 'jobs', 'careers', 'presse', 'press',
        'marketing', 'admin', 'webmaster', 'noreply', 'no-reply', 'contactez'
    }
    emails_nominatifs = [e for e in emails_uniques if e.split('@')[0] not in prefixes_generiques]
    emails_generiques = [e for e in emails_uniques if e.split('@')[0] in prefixes_generiques]

    patterns_trouves = [detecter_pattern_email(e.split('@')[0]) for e in emails_nominatifs]
    patterns_trouves = [p for p in patterns_trouves if p]
    pattern_detecte = None
    if patterns_trouves:
        comptes = {}
        for p in patterns_trouves:
            comptes[p] = comptes.get(p, 0) + 1
        pattern_detecte = max(comptes, key=comptes.get)

    return {
        'pattern_detecte': pattern_detecte,
        'email_generique': emails_generiques[0] if emails_generiques else None,
        'exemples_nominatifs': emails_nominatifs[:5],
    }


def ping_smtp(email, mx_server):
    """Se connecte au VRAI serveur mail (mx_server, résolu au préalable via
    get_mx_record) pour vérifier l'existence de l'e-mail - PAS au nom de domaine
    directement, qui est souvent le serveur du site web plutôt que celui des emails.
    Renvoie un message précis selon le type d'échec, plutôt qu'un message générique,
    pour pouvoir distinguer un blocage réseau local d'un vrai rejet du serveur cible.

    BUG CORRIGÉ : MAIL FROM utilisait un domaine expéditeur INVENTÉ, que certains
    serveurs rejettent explicitement car il n'existe pas réellement. Corrigé avec
    l'expéditeur "null" (MAIL FROM:<>), convention standard RFC 5321 pour ce type
    de sonde. Le code de retour de MAIL FROM est aussi vérifié avant d'envoyer
    RCPT, pour éviter un résultat incohérent si l'expéditeur est rejeté."""
    domaine = email.split('@')[-1]
    if domaine == "entreprise.com":
        return "Impossible (Domaine inconnu)"
    if not mx_server:
        return "Impossible (aucun serveur MX résolu pour ce domaine)"

    try:
        server = smtplib.SMTP(timeout=8)
        server.connect(mx_server, 25)
        server.helo("verification-bot.com")

        code_expediteur, msg_expediteur = server.mail("")  # MAIL FROM:<> (expéditeur null)
        if code_expediteur not in (250, 251):
            server.quit()
            msg_txt = msg_expediteur.decode(errors='ignore') if isinstance(msg_expediteur, bytes) else msg_expediteur
            return f"Expéditeur rejeté par le serveur (code {code_expediteur} : {msg_txt}) - vérification impossible"

        code, message = server.rcpt(email)
        server.quit()

        if code == 250:
            return "Valide (SMTP 250)"
        elif code == 550:
            return "Inexistant (SMTP 550)"
        else:
            return f"Incertain (Code {code} : {message.decode(errors='ignore') if isinstance(message, bytes) else message})"

    except (socket.timeout, TimeoutError):
        return "Timeout (le port 25 est probablement bloqué par votre réseau/hébergeur)"
    except ConnectionRefusedError:
        return "Connexion refusée (port 25 fermé côté serveur cible ou bloqué par votre réseau)"
    except smtplib.SMTPServerDisconnected:
        return "Le serveur a coupé la connexion (blocage anti-spam probable côté entreprise)"
    except smtplib.SMTPResponseException as e:
        return f"Rejet SMTP explicite (code {e.smtp_code})"
    except OSError as e:
        return f"Erreur réseau : {e}"
    except Exception as e:
        return f"Échec inattendu ({type(e).__name__})"


def initialiser_fichiers():
    """Crée l'en-tête du fichier de sortie si absent OU si le fichier est vide."""
    # BUG CORRIGÉ : os.path.exists() renvoie True même si le fichier a 0 octet
    # (ex: créé à la main sous Excel). On vérifie donc aussi la taille.
    if not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0:
        with open(OUTPUT_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "URL", "Prenom", "Nom", "Entreprise", "Domaine", "Source_Domaine",
                "Pattern_Detecte", "Source_Pattern",
                "Email_Plus_Probable", "Resultat_SMTP_Plus_Probable",
                "Email_Verifie_SMTP", "Resultat_SMTP_Verifie", "Nb_Formats_Testes",
                "Email_Generique_Trouve"
            ])


def determiner_delimiteur(filepath, encodage):
    """Détecte le séparateur réellement utilisé (',' ';' ou tab) en comptant
    les occurrences sur la 1ère ligne, plutôt que de simplement tester ';' in ligne
    (ce qui donnait un faux positif dès qu'un nom de colonne contenait un point-virgule
    ou, à l'inverse, ratait les fichiers ';' sans le détecter correctement)."""
    with open(filepath, mode='r', newline='', encoding=encodage) as f:
        premiere_ligne = f.readline()

    candidats = [',', ';', '\t']
    comptes = {c: premiere_ligne.count(c) for c in candidats}
    meilleur = max(comptes, key=comptes.get)

    # Si aucun séparateur n'apparaît, on retombe sur la virgule par défaut
    if comptes[meilleur] == 0:
        return ','
    return meilleur


def lire_csv_avec_encodage_securise(filepath):
    """Tente de lire le CSV avec détection automatique de l'encodage et du séparateur."""
    encodages = ['utf-8-sig', 'utf-8', 'cp1252', 'latin1']
    derniere_erreur = None
    for encodage in encodages:
        try:
            delimiteur = determiner_delimiteur(filepath, encodage)
            with open(filepath, mode='r', newline='', encoding=encodage) as f:
                reader = csv.DictReader(f, delimiter=delimiteur)
                lignes = list(reader)
                fieldnames = reader.fieldnames
                return lignes, fieldnames, encodage, delimiteur
        except (UnicodeDecodeError, Exception) as e:
            derniere_erreur = e
            continue
    raise UnicodeDecodeError(f"Impossible de lire le fichier. Dernière erreur : {derniere_erreur}", b"", 0, 1, "")


def trouver_valeur_colonne(ligne, mots_cles, exclure_cles=None):
    """Cherche une clé dans le dictionnaire sans se soucier des majuscules ou espaces invisibles.
    exclure_cles permet d'ignorer une colonne déjà identifiée pour un autre usage
    (ex: ne pas reprendre la colonne 'URL entreprise' comme colonne 'URL profil')."""
    exclure_cles = exclure_cles or []
    for cle, valeur in ligne.items():
        if cle and cle not in exclure_cles and any(mot in cle.strip().lower() for mot in mots_cles):
            return cle, valeur
    return None, ""


def trouver_colonne_nom_entreprise(ligne, exclure_cles=None):
    """
    NOUVEAU : cherche la colonne contenant le NOM (texte) de l'entreprise, ex:
    "Nom Company", "Nom Entreprise", "Société". Distincte de la colonne URL LinkedIn
    entreprise (qui contient un lien, pas un nom) : on l'exclut explicitement via
    exclure_cles, ET on vérifie que la valeur trouvée ne ressemble pas elle-même à une
    URL (sécurité supplémentaire si les en-têtes se ressemblent trop pour être
    distingués autrement)."""
    exclure_cles = exclure_cles or []
    mots_cles = ["nom entreprise", "nom societe", "nom company", "raison sociale",
                 "societe", "entreprise", "company", "nom"]
    for mot in mots_cles:
        for cle, valeur in ligne.items():
            if not cle or cle in exclure_cles:
                continue
            libelle = normaliser_libelle(cle)
            if re.search(rf'\b{re.escape(mot)}\b', libelle):
                valeur = (valeur or "").strip()
                if valeur and not re.match(r'^https?://', valeur, flags=re.IGNORECASE) and "linkedin.com" not in valeur.lower():
                    return cle, valeur
                elif not valeur:
                    return cle, valeur  # colonne identifiée mais vide pour cette ligne
    return None, ""


def executer_enrichissement():
    initialiser_fichiers()

    if not os.path.exists(INPUT_FILE):
        print(f"Erreur : Le fichier {INPUT_FILE} est introuvable dans le dossier courant : {os.getcwd()}")
        return

    try:
        lignes, fieldnames, encodage_detecte, delimiteur_detecte = lire_csv_avec_encodage_securise(INPUT_FILE)
        print(f"Fichier lu (Encodage: {encodage_detecte} | Séparateur: '{delimiteur_detecte}')")
        print(f"Colonnes détectées : {fieldnames}")
        print(f"Nombre de lignes lues : {len(lignes)}")
    except Exception as e:
        print(f"Erreur lors de la lecture du fichier : {e}")
        return

    if not lignes:
        print("Le fichier d'entrée ne contient aucune ligne de données (seulement l'en-tête, ou vide).")
        return

    print(f"Démarrage de l'extraction pour {len(lignes)} lignes...")

    nb_traitees = 0
    nb_ignorees_status = 0
    nb_ignorees_url_vide = 0
    cache_catch_all = {}      # évite de re-tester le même domaine à chaque ligne
    cache_analyse_site = {}   # évite de re-scraper le même site à chaque ligne
    cache_recherche_web = {}  # évite de re-chercher le même domaine sur le web à chaque ligne
    cache_mx = {}             # évite de re-résoudre le MX du même domaine à chaque ligne
    cache_site_entreprise = {}  # NOUVEAU : évite de re-chercher le site officiel de la même entreprise

    for index, ligne in enumerate(lignes):
        # 1) Colonne SITE WEB de l'entreprise - la source la plus fiable, prioritaire sur tout
        #    (si vous l'avez déjà, elle est utilisée telle quelle, aucune recherche n'est faite).
        cle_site_web, site_web_brut = trouver_valeur_colonne(
            ligne, ["site web", "site internet", "website", "domaine"]
        )
        site_web_brut = corriger_mojibake(site_web_brut.strip()) if site_web_brut else ""

        # 2) Colonne URL de l'ENTREPRISE (LinkedIn société) - utilisée pour la recherche
        #    du site officiel, et en repli pour deviner le nom si la colonne Nom (ci-dessous)
        #    est absente/vide.
        cle_url_entreprise, url_entreprise_brute = trouver_valeur_colonne(
            ligne, ["entreprise", "company", "société", "societe"],
            exclure_cles=[cle_site_web]
        )
        url_entreprise_brute = corriger_mojibake(url_entreprise_brute.strip()) if url_entreprise_brute else ""

        # 2bis) NOUVEAU : Colonne NOM (texte) de l'entreprise, ex: "Nom Company".
        #       Distincte de la colonne URL entreprise ci-dessus (exclue explicitement).
        cle_nom_entreprise, nom_entreprise_brut = trouver_colonne_nom_entreprise(
            ligne, exclure_cles=[cle_site_web, cle_url_entreprise]
        )
        nom_entreprise_brut = corriger_mojibake(nom_entreprise_brut.strip()) if nom_entreprise_brut else ""

        # 3) Colonne URL du PROFIL personnel (gère "url", "lien", "linkedin", "profil")
        cle_url, url_brute = trouver_valeur_colonne(
            ligne, ["url", "lien", "linkedin", "profil"],
            exclure_cles=[cle_url_entreprise, cle_site_web, cle_nom_entreprise]
        )
        url_brute = corriger_mojibake(url_brute.strip()) if url_brute else ""

        # Recherche souple de la colonne Status (gère "status", "statut", "Status")
        cle_status, status_actuel = trouver_valeur_colonne(ligne, ["status", "statut"])
        status_actuel = status_actuel.strip() if status_actuel else ""

        # Si aucune colonne de statut n'existe, on va la créer
        if not cle_status:
            cle_status = "Status"
            ligne[cle_status] = ""
            if "Status" not in fieldnames:
                fieldnames.append("Status")

        # Passer si déjà traité
        if status_actuel.lower() in ["traite", "traité"]:
            nb_ignorees_status += 1
            continue

        # Passer si la ligne n'a pas d'URL exploitable
        if not url_brute:
            nb_ignorees_url_vide += 1
            print(f"[{index+1}/{len(lignes)}] Ignoré : aucune URL trouvée sur cette ligne. "
                  f"Colonne URL détectée : {cle_url!r}. Contenu de la ligne : {ligne}")
            continue

        url_propre = formater_url(url_brute)
        print(f"\n[{index+1}/{len(lignes)}] Traitement de : {url_propre}")

        # 1. Prénom/Nom : en priorité le nom EXACT trouvé via une recherche web sur
        #    l'URL du profil (le titre LinkedIn indexé), plus fiable que de deviner
        #    depuis le slug de l'URL. Repli sur l'extraction depuis l'URL si la
        #    recherche ne trouve rien (site non indexé, requête bloquée, etc.).
        print(" -> Recherche du nom exact de la personne sur LinkedIn...")
        time.sleep(random.uniform(1.0, 2.0))
        prenom_recherche, nom_recherche = rechercher_nom_depuis_linkedin(url_propre)
        if prenom_recherche:
            prenom, nom = prenom_recherche, nom_recherche
            print(f" -> Nom exact trouvé via recherche web : {prenom} {nom}")
        else:
            prenom, nom = extraire_identité_depuis_url(url_propre)
            print(f" -> Nom exact introuvable via recherche, repli sur l'URL : {prenom} {nom}")
        prenom, nom = corriger_mojibake(prenom), corriger_mojibake(nom)

        # 2. Détermination du domaine, par ordre de fiabilité décroissant :
        #    a) site web fourni directement dans le CSV -> domaine réel, aucune estimation
        #    b) nom d'entreprise (colonne Nom, ou à défaut extrait de l'URL LinkedIn societé)
        #       -> RECHERCHE du vrai site officiel (page LinkedIn puis recherche web), et
        #       seulement si rien n'est trouvé, on revient à la devinette nom + ".com"
        #    c) recherche DuckDuckGo sur la personne -> tout est estimé (dernier recours)
        domaine = None
        entreprise = None
        source_domaine = ""

        if site_web_brut:
            domaine = extraire_domaine_depuis_site_web(site_web_brut)
            if domaine:
                source_domaine = "Site web fourni"
                print(f" -> Domaine (fourni directement) : {domaine}")

        if not domaine and (nom_entreprise_brut or url_entreprise_brute):
            entreprise = nom_entreprise_brut or extraire_entreprise_depuis_url(url_entreprise_brute)
            if entreprise:
                print(f" -> Entreprise : {entreprise}")
                cle_cache = (entreprise.strip().lower(), url_entreprise_brute.strip().lower())
                if cle_cache not in cache_site_entreprise:
                    print(f" -> Recherche du site officiel de {entreprise}...")
                    cache_site_entreprise[cle_cache] = rechercher_site_web_entreprise(entreprise, url_entreprise_brute)
                domaine_trouve, source_recherche = cache_site_entreprise[cle_cache]

                if domaine_trouve:
                    domaine = domaine_trouve
                    source_domaine = f"Site officiel trouvé ({source_recherche})"
                    print(f" -> Site officiel trouvé : {domaine} [{source_recherche}]")
                else:
                    domaine = deviner_domaine_depuis_nom_entreprise(entreprise)
                    source_domaine = "Estimé depuis nom entreprise (site introuvable)"
                    print(f" -> Site officiel introuvable, domaine estimé : {domaine}")

        if not domaine:
            entreprise, domaine = deviner_entreprise_et_domaine(prenom, nom)
            source_domaine = "Estimé (recherche DuckDuckGo)"
            print(f" -> Identité trouvée : {prenom} {nom}")
            print(f" -> Société estimée (recherche) : {entreprise} (Domaine : {domaine})")

        if not entreprise:
            entreprise = "Entreprise Inconnue"

        # 3. AVANT de générer les candidats : on interroge le site web de l'entreprise
        #    pour savoir s'il révèle sa vraie structure d'email (ex: emails nominatifs
        #    trouvés sur une page équipe/à propos) et/ou confirme le domaine mail réel.
        #    Un seul scraping par domaine, mis en cache pour tout le run.
        analyse_site = {'domaine_confirme': None, 'pattern_detecte': None,
                         'email_generique': None, 'exemples_nominatifs': []}

        if domaine != "entreprise.com":
            if domaine not in cache_analyse_site:
                print(f" -> Analyse du site web de {domaine} (structure des emails)...")
                cache_analyse_site[domaine] = analyser_site_web_entreprise(domaine)
            analyse_site = cache_analyse_site[domaine]

            if analyse_site['domaine_confirme'] and analyse_site['domaine_confirme'] != domaine:
                print(f" -> Domaine mail réel confirmé via le site : "
                      f"{analyse_site['domaine_confirme']} (au lieu de {domaine})")
                domaine = analyse_site['domaine_confirme']
                source_domaine += " (confirmé via site web)"

            if analyse_site['pattern_detecte']:
                print(f" -> Structure détectée sur le site : {analyse_site['pattern_detecte']} "
                      f"(exemples : {analyse_site['exemples_nominatifs'][:2]})")
            elif analyse_site['email_generique']:
                print(f" -> Aucune structure nominative trouvée, mais email générique repéré : "
                      f"{analyse_site['email_generique']}")

        # 3bis. Si le site propre de l'entreprise n'a rien révélé, on élargit la
        #       recherche à tout le web (communiqués, annuaires, actes de conférence...).
        #       Dès qu'on trouve une structure, on l'utilise et on arrête de chercher.
        pattern_final = analyse_site['pattern_detecte']
        email_generique_final = analyse_site['email_generique']
        source_pattern = "Site de l'entreprise" if pattern_final else ""

        if domaine != "entreprise.com" and not pattern_final:
            if domaine not in cache_recherche_web:
                print(f" -> Rien trouvé sur le site propre : recherche élargie sur le web...")
                cache_recherche_web[domaine] = rechercher_email_domaine_sur_web(domaine)
            analyse_web = cache_recherche_web[domaine]

            if analyse_web['pattern_detecte']:
                pattern_final = analyse_web['pattern_detecte']
                source_pattern = "Recherche web"
                print(f" -> Structure détectée via recherche web : {pattern_final} "
                      f"(exemples : {analyse_web['exemples_nominatifs'][:2]})")
            elif analyse_web['email_generique'] and not email_generique_final:
                email_generique_final = analyse_web['email_generique']
                print(f" -> Email générique trouvé via recherche web : {email_generique_final}")
            else:
                print(" -> Aucun email exploitable trouvé, même en recherche élargie.")

        # 4. Génération des formats d'e-mail candidats, du plus probable au moins probable.
        #    Si une structure a été détectée (site propre ou recherche web), elle passe en tête.
        candidats = generer_patterns_emails(
            prenom, nom, domaine, pattern_priorite=pattern_final
        )


        if not candidats:
            print(" -> Impossible de générer un e-mail (prénom/nom manquant).")
            continue

        email_plus_probable = candidats[0][1]
        print(f" -> Email le plus probable : {email_plus_probable}")

        # 5. Vérification SMTP : on teste les formats un par un et on s'arrête
        #    dès qu'un format répond "Valide". Le domaine "entreprise.com" (placeholder
        #    d'échec) n'est jamais testé pour ne pas perdre de temps.
        email_verifie = ""
        resultat_verifie = ""
        resultat_ping_probable = ""
        nb_testes = 0

        if domaine == "entreprise.com":
            resultat_ping_probable = "Impossible (Domaine inconnu)"
            resultat_verifie = "Impossible (Domaine inconnu)"
        else:
            # Résolution du VRAI serveur mail (MX) - un seul lookup par domaine, mis
            # en cache. AVANT cette correction, le script se connectait directement
            # au nom de domaine, souvent le serveur du site web plutôt que celui des
            # emails, ce qui faisait échouer la vérification pour une mauvaise raison.
            if domaine not in cache_mx:
                print(f" -> Résolution du serveur mail (MX) de {domaine}...")
                mx_server, erreur_mx = get_mx_record(domaine)
                cache_mx[domaine] = mx_server
                if mx_server:
                    print(f" -> Serveur MX trouvé : {mx_server}")
                else:
                    print(f" -> Échec de résolution MX : {erreur_mx}")
            mx_server = cache_mx[domaine]

            if not mx_server:
                resultat_ping_probable = "Impossible (résolution MX échouée)"
                resultat_verifie = "Impossible (résolution MX échouée)"
            else:
                # Détection catch-all : un seul test par domaine, mis en cache pour tout le run.
                if domaine not in cache_catch_all:
                    print(f" -> Vérification du mode catch-all sur {domaine}...")
                    cache_catch_all[domaine] = detecter_catch_all(domaine, mx_server)

                if cache_catch_all[domaine]:
                    print(" -> Domaine en mode catch-all : la vérification SMTP n'est pas fiable ici.")
                    resultat_ping_probable = "Catch-all détecté (résultat SMTP non fiable)"
                    resultat_verifie = "Catch-all détecté (résultat SMTP non fiable)"
                else:
                    print(" -> Lancement de la vérification SMTP (par ordre de probabilité)...")
                    for label, email in candidats:
                        resultat = ping_smtp(email, mx_server)
                        nb_testes += 1
                        print(f"    [{label}] {email} -> {resultat}")

                        if nb_testes == 1:
                            resultat_ping_probable = resultat

                        if resultat == "Valide (SMTP 250)":
                            email_verifie = email
                            resultat_verifie = resultat
                            break

                    if not email_verifie:
                        resultat_verifie = "Aucun format validé par SMTP"

        print(f" -> Résultat pour le format le plus probable : {resultat_ping_probable}")
        if email_verifie:
            print(f" -> Email validé par SMTP : {email_verifie}")
        else:
            print(" -> Aucun format n'a pu être validé par SMTP (serveur bloquant, catch-all, ou adresse inexistante).")

        # Enregistrement du lead enrichi
        with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f_out:
            writer = csv.writer(f_out)
            writer.writerow([
                url_propre, prenom, nom, entreprise, domaine, source_domaine,
                pattern_final, source_pattern,
                email_plus_probable, resultat_ping_probable,
                email_verifie, resultat_verifie, nb_testes,
                email_generique_final
            ])

        nb_traitees += 1

        # Mise à jour et sauvegarde en temps réel
        ligne[cle_status] = "Traité"
        with open(INPUT_FILE, mode='w', newline='', encoding=encodage_detecte) as f_in:
            writer = csv.DictWriter(f_in, fieldnames=fieldnames, delimiter=delimiteur_detecte)
            writer.writeheader()
            writer.writerows(lignes)

        # Limite de lot atteinte : on s'arrête proprement ici. La progression est
        # déjà sauvegardée (ligne par ligne, ci-dessus), donc une reprise ultérieure
        # repartira automatiquement des lignes non encore marquées "Traité".
        if MAX_LEADS_PAR_RUN and nb_traitees >= MAX_LEADS_PAR_RUN:
            print(f"\n--- Limite de {MAX_LEADS_PAR_RUN} lead(s) par exécution atteinte ---")
            break

    # Vérifie s'il reste des lignes non traitées, pour permettre à un orchestrateur
    # externe (ex: workflow GitHub Actions) de décider s'il faut relancer un lot.
    lignes_restantes = 0
    for ligne in lignes:
        _, url_verif = trouver_valeur_colonne(ligne, ["url", "lien", "linkedin", "profil"])
        _, status_verif = trouver_valeur_colonne(ligne, ["status", "statut"])
        if (url_verif or "").strip() and (status_verif or "").strip().lower() not in ["traite", "traité"]:
            lignes_restantes += 1

    print("\n--- Résumé ---")
    print(f"Lignes traitées avec succès (cette exécution) : {nb_traitees}")
    print(f"Lignes ignorées (déjà 'Traité') : {nb_ignorees_status}")
    print(f"Lignes ignorées (pas d'URL) : {nb_ignorees_url_vide}")
    print(f"Lignes restant à traiter : {lignes_restantes}")
    print("Fait !")

    return lignes_restantes


if __name__ == "__main__":
    restantes = executer_enrichissement()
    # Code de sortie 2 = il reste du travail (utile pour qu'un orchestrateur externe
    # sache s'il doit relancer un nouveau lot). Code 0 = tout est terminé.
    if restantes:
        raise SystemExit(2)