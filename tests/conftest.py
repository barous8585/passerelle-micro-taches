"""
conftest.py — s'exécute avant la collecte des tests.

Supprime la base SQLite existante pour repartir d'un état propre à chaque
exécution de la suite (les tests d'intégration dans test_api.py partagent
un état construit au fil des tests, comme lors des vérifications manuelles
faites pendant le développement).
"""

import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RACINE)

DB_PATH = os.path.join(RACINE, "passerelle.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
