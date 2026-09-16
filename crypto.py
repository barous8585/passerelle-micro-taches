"""
crypto.py — Chiffrement au repos des données fournies par les clients
(lignes brutes, résultats consolidés, réponses des workers) avant stockage
en base de données.

Protège contre le scénario le plus réaliste pour ce type de plateforme :
quelqu'un récupère une copie du fichier SQLite (ordinateur volé, sauvegarde
mal configurée, dump accidentel) et se retrouve avec du texte chiffré
illisible plutôt que les données métier des clients en clair.

Contrairement aux notifications Telegram (best-effort, désactivables), la
clé de chiffrement est OBLIGATOIRE : sans elle, le serveur refuse de
démarrer plutôt que de stocker silencieusement des données en clair sans
que personne ne s'en aperçoive.
"""

import json
import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

_CLE = os.environ.get("APP_ENCRYPTION_KEY")
if not _CLE:
    raise RuntimeError(
        "APP_ENCRYPTION_KEY manquante dans .env -- indispensable pour chiffrer "
        "les données clients au repos. Génère-en une avec :\n"
        '  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
        "puis colle le résultat dans .env sous la forme APP_ENCRYPTION_KEY=<valeur>"
    )

_fernet = Fernet(_CLE.encode())


def chiffrer_json(valeur) -> str:
    """Sérialise en JSON puis chiffre -- résultat stockable dans une colonne texte."""
    return _fernet.encrypt(json.dumps(valeur).encode()).decode()


def dechiffrer_json(valeur_chiffree):
    """Inverse de chiffrer_json. None reste None (ligne pas encore traitée).
    Ne masque jamais une erreur de déchiffrement -- mieux vaut planter
    bruyamment que renvoyer un résultat silencieusement faux sur des
    données clients."""
    if valeur_chiffree is None:
        return None
    return json.loads(_fernet.decrypt(valeur_chiffree.encode()).decode())
