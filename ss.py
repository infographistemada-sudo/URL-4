import csv
import json
import re
import time
import urllib.parse
import urllib.request
import ssl

# --- CONFIGURATION ---
FICHIER_ENTREE = "entreprises.csv"  # Nom de votre fichier de départ
FICHIER_SORTIE = "resultats_complets.tsv"  # Contient : Nom, SIRET, Dirigeant

# L'API impose 7 appels/seconde max - on reste large en-dessous.
PAUSE_ENTRE_APPELS = 0.3


def nettoyer_texte(valeur):
    """Retire les retours à la ligne/tabulations parasites et réduit les
    espaces multiples à un seul - évite d'envoyer une requête bizarre à
    l'API si un champ du CSV contient un saut de ligne caché."""
    if not valeur:
        return ""
    return re.sub(r'\s+', ' ', valeur).strip()


def chercher_infos_entreprise(nom, adresse=""):
    # On nettoie la requête pour l'API
    requete = nettoyer_texte(f"{nom} {adresse}")
    query_encodee = urllib.parse.quote(requete)

    # BUG CORRIGÉ : il manquait le vrai nom d'hôte de l'API et le séparateur
    # "/search?q=" entre le domaine et la requête. Avant, l'URL ressemblait à
    # "https://api.gouv.frSpa Léonard de Vinci...&per_page=1" - Python essayait
    # alors d'interpréter tout le texte de recherche comme un nom d'hôte, d'où
    # l'erreur "URL can't contain control characters".
    url = f"https://recherche-entreprises.api.gouv.fr/search?q={query_encodee}&per_page=1"

    siret = "Non trouvé"
    dirigeant = "Non trouvé"

    try:
        # Contournement des problèmes de certificats SSL sous certaines versions de Windows
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            url, headers={"User-Agent": "ScriptAutomatisationSIRET/1.0"}
        )
        with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

            # Vérification de la présence de résultats
            if data.get("results") and len(data["results"]) > 0:
                premier_resultat = data["results"][0]

                # 1. Extraction du SIRET
                etablissements = premier_resultat.get("matching_etablissements", [])
                if etablissements and len(etablissements) > 0:
                    siret = etablissements[0].get("siret", "Non trouvé")
                else:
                    siret = premier_resultat.get("siege", {}).get("siret", "Non trouvé")

                # 2. Extraction du Dirigeant Principal
                liste_dirigeants = premier_resultat.get("dirigeants", [])
                if liste_dirigeants and len(liste_dirigeants) > 0:
                    p_dirigeant = liste_dirigeants[0]

                    # BUG CORRIGÉ : l'API renvoie "prenoms" (au pluriel), pas
                    # "prenom" - avec l'ancien nom de champ, le prénom était
                    # toujours vide, même quand la requête aboutissait.
                    if p_dirigeant.get("nom") or p_dirigeant.get("prenoms"):
                        nom_p = (p_dirigeant.get("nom") or "").upper()
                        prenom_p = (p_dirigeant.get("prenoms") or "").split(" ")[0].title()
                        dirigeant = f"{prenom_p} {nom_p}".strip()
                    elif p_dirigeant.get("denomination"):
                        dirigeant = p_dirigeant.get("denomination")

    except Exception as e:
        # Permet d'afficher la vraie cause de l'erreur dans la console pour débugger
        print(f"  [!] Erreur technique sur cette ligne : {e}")
        siret = "Erreur API"
        dirigeant = "Erreur API"

    return siret, dirigeant


def main():
    print(f"Début du traitement de '{FICHIER_ENTREE}'...")

    try:
        with (
            open(FICHIER_ENTREE, "r", encoding="cp1252", errors="ignore") as f_in,
            open(
                FICHIER_SORTIE, "w", encoding="utf-8", newline=""
            ) as f_out,
        ):

            # Détection automatique du séparateur
            contenu_debut = f_in.read(2048)
            f_in.seek(0)

            separateur = "\t"  # Par défaut
            for sep in [";", ",", "\t"]:
                if sep in contenu_debut:
                    separateur = sep
                    break

            print(f"-> Séparateur détecté : '{separateur.replace(chr(9), chr(92) + 't')}'")

            lecteur = csv.reader(f_in, delimiter=separateur)
            ecrivain = csv.writer(f_out, delimiter="\t")

            lignes_traitees = 0

            for ligne in lecteur:
                if not ligne or len(ligne) == 0 or not ligne[0].strip():
                    continue

                # On prend la première colonne comme nom de société
                nom_societe = nettoyer_texte(ligne[0])

                # S'il y a une deuxième colonne et qu'elle ne contient pas déjà une ancienne erreur, on la prend comme adresse
                adresse = ""
                if len(ligne) >= 2 and "Erreur API" not in ligne[1]:
                    adresse = nettoyer_texte(ligne[1])

                # On ignore la ligne d'en-tête si elle existe
                if nom_societe.lower() in ["nom_societe", "nom société", "nom"]:
                    continue

                print(f"Recherche pour : {nom_societe}...")

                siret, dirigeant = chercher_infos_entreprise(
                    nom_societe, adresse
                )

                print(f"    -> SIRET : {siret} | Dirigeant : {dirigeant}")

                # Écriture dans le fichier final (sans en-tête)
                ecrivain.writerow([nom_societe, siret, dirigeant])
                f_out.flush()
                lignes_traitees += 1

                # Légère pause pour l'API
                time.sleep(PAUSE_ENTRE_APPELS)

        print(
            f"\nTerminé ! {lignes_traitees} lignes ont été traitées avec succès."
        )
        print(f"Le fichier résultat a été enregistré sous : '{FICHIER_SORTIE}'")

    except FileNotFoundError:
        print(
            f"Erreur : Le fichier '{FICHIER_ENTREE}' est introuvable."
        )


if __name__ == "__main__":
    main()
