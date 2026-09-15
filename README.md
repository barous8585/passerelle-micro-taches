# Passerelle de Micro-Tâches Data

Prototype de plateforme mettant en relation des entreprises ayant des
données à nettoyer (data-cleaning, structuration de CSV) et des étudiants
rémunérés à la micro-tâche, avec contrôle qualité automatisé.

## Installation

```bash
python3 -m venv venv
source venv/bin/activate        # sous Windows : venv\Scripts\activate
pip install -r requirements.txt
```

## Lancer le serveur

```bash
uvicorn api:app --reload
```

- Dépôt client (upload CSV + inférence de schéma) : http://localhost:8000/
- Dashboard étudiant (traitement des tâches) : http://localhost:8000/worker
- Documentation interactive de l'API (générée par FastAPI) : http://localhost:8000/docs

## Lancer les tests

```bash
pytest -v
```

Les tests couvrent : l'inférence de schéma, la détection d'anomalies, le
consensus pondéré par le `trust_score`, le verrouillage anti-doublon des
tâches, l'authentification par rôle et le flux complet de bout en bout
(dépôt → découpage → distribution → paiement).

## Structure du projet

| Fichier | Rôle |
|---|---|
| `analyzer.py` | Détection d'anomalies, inférence de schéma, consensus pondéré |
| `models.py` | Modèles SQLAlchemy (users, projects, micro_tasks, submissions) |
| `auth.py` | Authentification par mot de passe (PBKDF2) et contrôle des rôles |
| `api.py` | Endpoints FastAPI (auth, dépôt client, distribution des tâches) |
| `client_upload.html` | Interface entreprise : dépôt de CSV |
| `worker_dashboard.html` | Interface étudiant : traitement des tâches |
| `tests/` | Suite de tests `pytest` |

## Statut

Prototype fonctionnel, testé de bout en bout. Restent hors périmètre :
paiement réel (Stripe Connect ou équivalent), tableau de bord admin pour
les décaissements, tâche planifiée de nettoyage des verrous expirés
(actuellement nettoyés à la demande).
