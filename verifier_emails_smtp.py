import csv
import os
import re
import socket
import smtplib
import time
import dns.resolver

# ==================== CONFIGURATION ====================
INPUT_FILE = "liste_mail.csv"
OUTPUT_FILE = "mails_verifies.csv"

# Nombre max d'emails vérifiés en une seule exécution (0 = pas de limite).
# Utile en CI (GitHub Actions) pour traiter par lots successifs et éviter de se
# faire bloquer par les serveurs mail à force de sonder trop d'adresses d'affilée
# depuis la même IP. La progression est sauvegardée ligne par ligne (colonne
# Status = "Traité"), donc une reprise ultérieure est toujours sûre.
MAX_EMAILS_PAR_RUN = int(os.environ.get("MAX_EMAILS_PAR_RUN", "0") or "0")

# Pause entre chaque vérification SMTP, pour rester "poli" avec les serveurs
# mail et éviter de déclencher un blocage anti-abus.
PAUSE_ENTRE_VERIFICATIONS = 1.0
# =======================================================


def determiner_delimiteur(filepath, encodage):
    """Détecte le séparateur réellement utilisé en comptant les occurrences des
    séparateurs les plus courants sur la 1ère ligne, avec repli sur csv.Sniffer
    pour les cas ambigus. Couvre virgule, point-virgule, tabulation et pipe -
    largement suffisant pour un fichier CSV/TSV réel."""
    with open(filepath, mode='r', newline='', encoding=encodage) as f:
        premiere_ligne = f.readline()
        echantillon = premiere_ligne + f.readline()  # 2 lignes pour csv.Sniffer

    candidats = [',', ';', '\t', '|']
    comptes = {c: premiere_ligne.count(c) for c in candidats}
    meilleur = max(comptes, key=comptes.get)

    if comptes[meilleur] > 0:
        return meilleur

    # Aucun des séparateurs courants trouvé : dernier recours avec csv.Sniffer,
    # qui peut détecter d'autres délimiteurs moins fréquents (ex: ':').
    try:
        dialecte = csv.Sniffer().sniff(echantillon, delimiters=",;\t|: ")
        return dialecte.delimiter
    except csv.Error:
        return ','


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
    """Cherche une clé dans le dictionnaire sans se soucier des majuscules ou
    espaces invisibles."""
    exclure_cles = exclure_cles or []
    for cle, valeur in ligne.items():
        if cle and cle not in exclure_cles and any(mot in cle.strip().lower() for mot in mots_cles):
            return cle, valeur
    return None, ""


def get_mx_record(domaine):
    """Résout le VRAI serveur mail (enregistrement MX) du domaine. Essaie d'abord
    des résolveurs publics (Google/Cloudflare), puis se replie sur le résolveur
    système par défaut si ceux-ci sont bloqués par le réseau local."""
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
    """Se connecte au VRAI serveur mail (mx_server) pour vérifier l'existence de
    l'e-mail. Renvoie un message précis selon le type d'échec.

    BUG CORRIGÉ : le script envoyait MAIL FROM avec un domaine expéditeur INVENTÉ
    ("test@verification-bot.com"), que certains serveurs rejettent explicitement
    car ce domaine n'existe pas réellement (ex: "Sender address rejected: Domain
    not found"). Corrigé en utilisant l'expéditeur "null" (MAIL FROM:<>), la
    convention standard RFC 5321 pour les sondes de vérification/non-livraison -
    bien mieux acceptée. Le code de retour de MAIL FROM est aussi maintenant
    vérifié avant d'envoyer RCPT : avant cette correction, un MAIL FROM rejeté en
    silence menait à un RCPT sans queue ni tête (ex: erreur 503 "Need MAIL before
    RCPT"), qui n'a rien à voir avec la validité de l'email testé."""
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


def detecter_catch_all(domaine, mx_server):
    """Teste si le serveur mail du domaine accepte N'IMPORTE QUELLE adresse
    (mode 'catch-all'). Si oui, un résultat 'Valide' via RCPT TO ne prouve rien."""
    import uuid
    faux_local_part = f"verif-inexistante-{uuid.uuid4().hex[:10]}"
    resultat = ping_smtp(f"{faux_local_part}@{domaine}", mx_server)
    return resultat == "Valide (SMTP 250)"


def initialiser_fichiers():
    """Crée l'en-tête du fichier de sortie si absent OU si le fichier est vide."""
    if not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0:
        with open(OUTPUT_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Email", "Domaine", "Serveur_MX", "Catch_All",
                "Resultat_SMTP", "Statut_Final"
            ])


def executer_verification():
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
        cle_email, email_brut = trouver_valeur_colonne(ligne, ["email", "mail", "e-mail", "adresse"])
        email = (email_brut or "").strip().lower()

        cle_status, status_actuel = trouver_valeur_colonne(ligne, ["status", "statut"])
        status_actuel = (status_actuel or "").strip()

        if not cle_status:
            cle_status = "Status"
            ligne[cle_status] = ""
            if "Status" not in fieldnames:
                fieldnames.append("Status")

        if status_actuel.lower() in ["traite", "traité"]:
            nb_ignorees_status += 1
            continue

        if not email or '@' not in email or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            nb_ignorees_format += 1
            print(f"[{index+1}/{len(lignes)}] Ignoré : format d'email invalide ou vide -> {email_brut!r}")
            # On marque quand même la ligne comme traitée, sinon un email au format
            # invalide reste "à traiter" pour toujours et déclenche des runs GitHub
            # Actions à l'infini sans jamais rien pouvoir vérifier.
            with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f_out:
                writer = csv.writer(f_out)
                writer.writerow([email_brut or "", "", "", "", "", "Ignoré (format invalide)"])
            ligne[cle_status] = "Traité"
            with open(INPUT_FILE, mode='w', newline='', encoding=encodage_detecte) as f_in:
                writer = csv.DictWriter(f_in, fieldnames=fieldnames, delimiter=delimiteur_detecte)
                writer.writeheader()
                writer.writerows(lignes)
            continue

        domaine = email.split('@')[-1]
        print(f"\n[{index+1}/{len(lignes)}] Vérification de : {email}")

        # 1. Résolution MX (mise en cache par domaine)
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
            resultat_smtp = "Impossible (résolution MX échouée)"
            statut_final = "Impossible (MX)"
        else:
            # 2. Détection catch-all (mise en cache par domaine)
            if domaine not in cache_catch_all:
                print(f" -> Vérification du mode catch-all sur {domaine}...")
                cache_catch_all[domaine] = detecter_catch_all(domaine, mx_server)
                time.sleep(0.5)  # petite pause après le test catch-all

            if cache_catch_all[domaine]:
                print(" -> Domaine en mode catch-all : la vérification SMTP n'est pas fiable ici.")
                resultat_smtp = "Catch-all détecté (résultat non fiable)"
                statut_final = "Catch-all (non fiable)"
            else:
                resultat_smtp = ping_smtp(email, mx_server)
                print(f" -> Résultat SMTP : {resultat_smtp}")
                if resultat_smtp == "Valide (SMTP 250)":
                    statut_final = "Valide"
                elif resultat_smtp == "Inexistant (SMTP 550)":
                    statut_final = "Inexistant"
                elif resultat_smtp.startswith("Incertain"):
                    statut_final = "Incertain"
                else:
                    statut_final = "Erreur (voir Resultat_SMTP)"

        # Enregistrement du résultat
        with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f_out:
            writer = csv.writer(f_out)
            writer.writerow([
                email, domaine, mx_server or "",
                "Oui" if cache_catch_all.get(domaine) else ("Non" if mx_server else ""),
                resultat_smtp, statut_final
            ])

        nb_traitees += 1

        # Mise à jour et sauvegarde en temps réel (reprise sûre)
        ligne[cle_status] = "Traité"
        with open(INPUT_FILE, mode='w', newline='', encoding=encodage_detecte) as f_in:
            writer = csv.DictWriter(f_in, fieldnames=fieldnames, delimiter=delimiteur_detecte)
            writer.writeheader()
            writer.writerows(lignes)

        time.sleep(PAUSE_ENTRE_VERIFICATIONS)

        if MAX_EMAILS_PAR_RUN and nb_traitees >= MAX_EMAILS_PAR_RUN:
            print(f"\n--- Limite de {MAX_EMAILS_PAR_RUN} email(s) par exécution atteinte ---")
            break

    # Compte les lignes restant à traiter, pour permettre à un orchestrateur
    # externe (workflow GitHub Actions) de décider s'il faut relancer un lot.
    lignes_restantes = 0
    for ligne in lignes:
        _, email_verif = trouver_valeur_colonne(ligne, ["email", "mail", "e-mail", "adresse"])
        _, status_verif = trouver_valeur_colonne(ligne, ["status", "statut"])
        if (email_verif or "").strip() and (status_verif or "").strip().lower() not in ["traite", "traité"]:
            lignes_restantes += 1

    print("\n--- Résumé ---")
    print(f"Emails vérifiés (cette exécution) : {nb_traitees}")
    print(f"Ignorés (déjà 'Traité') : {nb_ignorees_status}")
    print(f"Ignorés (format invalide) : {nb_ignorees_format}")
    print(f"Emails restant à vérifier : {lignes_restantes}")
    print("Fait !")

    return lignes_restantes


if __name__ == "__main__":
    restantes = executer_verification()
    # Code de sortie 2 = il reste du travail, 0 = tout est terminé.
    if restantes:
        raise SystemExit(2)