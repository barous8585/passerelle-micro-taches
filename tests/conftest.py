"""
conftest.py — s'exécute avant la collecte des tests.

Supprime la base SQLite existante pour repartir d'un état propre à chaque
exécution de la suite (les tests d'intégration dans test_api.py partagent
un état construit au fil des tests, comme lors des vérifications manuelles
faites pendant le développement).
"""

import os
import sys

from cryptography.fernet import Fernet

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

# crypto.py exige une clé de chiffrement pour s'importer -- on en fournit une
# de test si aucune n'est déjà définie (ex: en local via .env). Cette clé
# n'a aucune valeur de sécurité réelle : la base de test est détruite à
# chaque exécution (voir plus bas).
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

DB_PATH = os.path.join(RACINE, "passerelle.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
