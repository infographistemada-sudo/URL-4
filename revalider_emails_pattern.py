import csv
import os
import re
import socket
import smtplib
import time
import unicodedata
import dns.resolver

# ==================== CONFIGURATION ====================
INPUT_FILE = "liste_a_revalider.csv"
OUTPUT_FILE = "emails_revalides.csv"

# Nombre max de lignes traitées en une seule exécution (0 = pas de limite).
MAX_PAR_RUN = int(os.environ.get("MAX_PAR_RUN", "0") or "0")

# Pause entre chaque vérification SMTP, pour rester "poli" avec les serveurs mail.
PAUSE_ENTRE_VERIFICATIONS = 1.0
# =======================================================


def corriger_mojibake(text):
    """Répare les accents cassés (ex: 'AurÃ©lie' -> 'Aurélie')."""
    if not text or ("Ã" not in text and "â€" not in text):
        return text
    try:
        return text.encode('cp1252').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def clean_string(text):
    """Nettoie le texte : minuscule, supprime les espaces et les accents."""
    if not text:
        return ""
    text = text.strip().lower()
    text = "".join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]', '', text)


def generer_patterns_emails(prenom, nom, domaine):
    """Génère une liste ordonnée (du plus probable au moins probable) de formats
    d'e-mail professionnels courants. Retourne une liste de tuples (label, email).
    Reprise à l'identique de la logique déjà validée dans enrichissement_leads.py."""
    p = clean_string(prenom)
    n = clean_string(nom).replace(" ", "")

    candidats = []
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

    vus = set()
    resultat = []
    for label, email in candidats:
        if email and email not in vus:
            vus.add(email)
            resultat.append((label, email))
    return resultat


def determiner_delimiteur(filepath, encodage):
    with open(filepath, mode='r', newline='', encoding=encodage) as f:
        premiere_ligne = f.readline()
        echantillon = premiere_ligne + f.readline()
    candidats = [',', ';', '\t', '|']
    comptes = {c: premiere_ligne.count(c) for c in candidats}
    meilleur = max(comptes, key=comptes.get)
    if comptes[meilleur] > 0:
        return meilleur
    try:
        dialecte = csv.Sniffer().sniff(echantillon, delimiters=",;\t|: ")
        return dialecte.delimiter
    except csv.Error:
        return ','


def lire_csv_avec_encodage_securise(filepath):
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
    exclure_cles = exclure_cles or []
    for cle, valeur in ligne.items():
        if cle and cle not in exclure_cles and any(mot in cle.strip().lower() for mot in mots_cles):
            return cle, valeur
    return None, ""


def get_mx_record(domaine):
    """Résout le VRAI serveur mail (enregistrement MX) du domaine. Essaie d'abord
    des résolveurs publics (Google/Cloudflare), puis se replie sur le résolveur
    système par défaut. Reprise à l'identique de la version déjà validée."""
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
            continue
    return None, "Échec DNS via toutes les méthodes (blocage réseau probable)"


def ping_smtp(email, mx_server):
    """Se connecte au VRAI serveur mail (mx_server) - avec expéditeur MAIL FROM:<>
    (null, conforme RFC 5321) plutôt qu'un domaine expéditeur inventé. Reprise à
    l'identique de la version déjà corrigée et validée."""
    if not mx_server:
        return "Impossible (aucun serveur MX résolu pour ce domaine)"

    try:
        server = smtplib.SMTP(timeout=8)
        server.connect(mx_server, 25)
        server.helo("verification-bot.com")

        code_expediteur, msg_expediteur = server.mail("")
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


def detecter_catch_all(domaine, mx_server):
    import uuid
    faux_local_part = f"verif-inexistante-{uuid.uuid4().hex[:10]}"
    resultat = ping_smtp(f"{faux_local_part}@{domaine}", mx_server)
    return resultat == "Valide (SMTP 250)"


def calculer_niveau_confiance(email_valide_trouve, mx_server):
    """Calcule un niveau de confiance à partir de ce que le SMTP a pu confirmer.

    Contrairement à enrichissement_leads.py (qui scrape le site web de l'entreprise
    et peut donc s'appuyer sur un email nominatif réellement observé comme preuve
    intermédiaire), ce script-ci ne teste QUE les formats par SMTP, sans aucune
    autre source de preuve - il n'y a donc pas d'équivalent aux niveaux "Moyen"
    et "Faible" de l'autre script (qui reposaient sur cette preuve de scraping).
    Seuls 3 niveaux sont possibles ici :
    - Élevé : un format a été confirmé positivement par SMTP
    - Très faible : la vérification a pu être tentée (MX résolu) mais rien n'a
      été confirmé (catch-all confirmé, ou tous les formats rejetés/incertains)
    - Indéterminé : la résolution MX elle-même a échoué, aucune vérification
      SMTP n'a même pu être tentée"""
    if not mx_server:
        return "Indéterminé"
    if email_valide_trouve:
        return "Élevé"
    return "Très faible"


def initialiser_fichiers():
    if not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0:
        with open(OUTPUT_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Prenom", "Nom", "Email_Original", "Resultat_Original", "Domaine",
                "Serveur_MX", "Catch_All", "Email_Valide_Trouve",
                "Resultat_Nouvelle_Verification", "Nb_Formats_Testes", "Niveau_Confiance"
            ])


def executer_revalidation():
    initialiser_fichiers()

    if not os.path.exists(INPUT_FILE):
        print(f"Erreur : Le fichier {INPUT_FILE} est introuvable dans le dossier courant : {os.getcwd()}")
        return 0

    try:
        lignes, fieldnames, encodage_detecte, delimiteur_detecte = lire_csv_avec_encodage_securise(INPUT_FILE)
        print(f"Fichier lu (Encodage: {encodage_detecte} | Séparateur: '{delimiteur_detecte}')")
        print(f"Colonnes détectées : {fieldnames}")
        print(f"Nombre de lignes lues : {len(lignes)}")
    except Exception as e:
        print(f"Erreur lors de la lecture du fichier : {e}")
        return 0

    if not lignes:
        print("Le fichier d'entrée ne contient aucune ligne de données.")
        return 0

    nb_traitees = 0
    nb_ignorees_status = 0
    nb_ignorees_format = 0
    cache_mx = {}
    cache_catch_all = {}

    for index, ligne in enumerate(lignes):
        # "prenom" doit être cherché AVANT "nom" et exclu de sa recherche, car
        # "prénom" contient littéralement la sous-chaîne "nom".
        cle_prenom, prenom_brut = trouver_valeur_colonne(ligne, ["prenom", "prénom", "first_name", "firstname"])
        prenom = corriger_mojibake((prenom_brut or "").strip())

        cle_nom, nom_brut = trouver_valeur_colonne(
            ligne, ["nom", "last_name", "lastname"], exclure_cles=[cle_prenom]
        )
        nom = corriger_mojibake((nom_brut or "").strip())

        cle_email, email_brut = trouver_valeur_colonne(ligne, ["email", "mail", "e-mail", "adresse"])
        email_original = (email_brut or "").strip().lower()

        cle_resultat_orig, resultat_orig = trouver_valeur_colonne(
            ligne, ["resultat_smtp", "resultat", "statut"]
        )
        resultat_original = (resultat_orig or "").strip()

        cle_status, status_actuel = trouver_valeur_colonne(ligne, ["status", "statut de traitement", "traite"])
        status_actuel = (status_actuel or "").strip()

        if not cle_status:
            cle_status = "Status_Revalidation"
            ligne[cle_status] = ""
            if "Status_Revalidation" not in fieldnames:
                fieldnames.append("Status_Revalidation")

        if status_actuel.lower() in ["traite", "traité"]:
            nb_ignorees_status += 1
            continue

        domaine = email_original.split('@')[-1] if '@' in email_original else ""

        if not prenom or not nom or not domaine:
            nb_ignorees_format += 1
            print(f"[{index+1}/{len(lignes)}] Ignoré : prénom/nom/domaine manquant -> "
                  f"prenom={prenom!r} nom={nom!r} email={email_original!r}")
            with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f_out:
                writer = csv.writer(f_out)
                writer.writerow([prenom, nom, email_original, resultat_original, domaine,
                                  "", "", "", "Ignoré (prénom/nom/domaine manquant)", 0, "Indéterminé"])
            ligne[cle_status] = "Traité"
            with open(INPUT_FILE, mode='w', newline='', encoding=encodage_detecte) as f_in:
                writer = csv.DictWriter(f_in, fieldnames=fieldnames, delimiter=delimiteur_detecte)
                writer.writeheader()
                writer.writerows(lignes)
            continue

        print(f"\n[{index+1}/{len(lignes)}] Revalidation de : {prenom} {nom} ({email_original}) "
              f"[résultat original : {resultat_original}]")

        # Génère les 8 formats standards, puis s'assure que l'email original fourni
        # est bien testé en premier même s'il ne correspond à aucun de ces formats
        # (ex: nom composé, format maison spécifique).
        candidats = generer_patterns_emails(prenom, nom, domaine)
        candidats = [("email fourni", email_original)] + [c for c in candidats if c[1] != email_original]

        # 1. Résolution MX (mise en cache par domaine) - avec NOTRE méthode corrigée
        if domaine not in cache_mx:
            print(f" -> Résolution du serveur mail (MX) de {domaine}...")
            mx_server, erreur_mx = get_mx_record(domaine)
            cache_mx[domaine] = mx_server
            if mx_server:
                print(f" -> Serveur MX trouvé : {mx_server}")
            else:
                print(f" -> Échec de résolution MX : {erreur_mx}")
        mx_server = cache_mx[domaine]

        email_valide = ""
        resultat_nouvelle_verif = ""
        nb_testes = 0

        if not mx_server:
            resultat_nouvelle_verif = "Impossible (résolution MX échouée)"
        else:
            if domaine not in cache_catch_all:
                print(f" -> Vérification du mode catch-all sur {domaine}...")
                cache_catch_all[domaine] = detecter_catch_all(domaine, mx_server)
                time.sleep(0.5)

            if cache_catch_all[domaine]:
                print(" -> Domaine en mode catch-all confirmé : aucun format ne peut être "
                      "distingué par SMTP, quel que soit le nombre de tentatives.")
                resultat_nouvelle_verif = "Catch-all confirmé (aucun format vérifiable par SMTP)"
            else:
                print(" -> Domaine PAS catch-all avec notre méthode : test des formats un par un...")
                for label, email in candidats:
                    resultat = ping_smtp(email, mx_server)
                    nb_testes += 1
                    print(f"    [{label}] {email} -> {resultat}")
                    if resultat == "Valide (SMTP 250)":
                        email_valide = email
                        resultat_nouvelle_verif = resultat
                        break
                    time.sleep(0.3)  # petite pause entre chaque format testé

                if not email_valide:
                    resultat_nouvelle_verif = "Aucun format validé par SMTP"

        if email_valide:
            print(f" -> ✅ Email valide trouvé : {email_valide}")
        else:
            print(f" -> Résultat : {resultat_nouvelle_verif}")

        niveau_confiance = calculer_niveau_confiance(email_valide, mx_server)
        print(f" -> Niveau de confiance : {niveau_confiance}")

        with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f_out:
            writer = csv.writer(f_out)
            writer.writerow([
                prenom, nom, email_original, resultat_original, domaine,
                mx_server or "", "Oui" if cache_catch_all.get(domaine) else ("Non" if mx_server else ""),
                email_valide, resultat_nouvelle_verif, nb_testes, niveau_confiance
            ])

        nb_traitees += 1

        ligne[cle_status] = "Traité"
        with open(INPUT_FILE, mode='w', newline='', encoding=encodage_detecte) as f_in:
            writer = csv.DictWriter(f_in, fieldnames=fieldnames, delimiter=delimiteur_detecte)
            writer.writeheader()
            writer.writerows(lignes)

        time.sleep(PAUSE_ENTRE_VERIFICATIONS)

        if MAX_PAR_RUN and nb_traitees >= MAX_PAR_RUN:
            print(f"\n--- Limite de {MAX_PAR_RUN} ligne(s) par exécution atteinte ---")
            break

    lignes_restantes = 0
    for ligne in lignes:
        _, email_verif = trouver_valeur_colonne(ligne, ["email", "mail", "e-mail", "adresse"])
        _, status_verif = trouver_valeur_colonne(ligne, ["status", "statut de traitement", "traite"])
        if (email_verif or "").strip() and (status_verif or "").strip().lower() not in ["traite", "traité"]:
            lignes_restantes += 1

    print("\n--- Résumé ---")
    print(f"Lignes revalidées (cette exécution) : {nb_traitees}")
    print(f"Ignorées (déjà 'Traité') : {nb_ignorees_status}")
    print(f"Ignorées (données manquantes) : {nb_ignorees_format}")
    print(f"Lignes restant à revalider : {lignes_restantes}")
    print("Fait !")

    return lignes_restantes


if __name__ == "__main__":
    restantes = executer_revalidation()
    if restantes:
        raise SystemExit(2)
