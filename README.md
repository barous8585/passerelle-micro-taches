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

## Chiffrement des données clients (obligatoire)

Les données brutes déposées par les clients (`raw_data`, `resultat_final`) et
les réponses des workers sont chiffrées avant stockage en base -- même en cas
de fuite du fichier `passerelle.db`, le contenu métier reste illisible sans
la clé.

1. Génère ta clé : `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Copie `.env.example` en `.env` et colle la clé dans `APP_ENCRYPTION_KEY`

**Sans cette clé, le serveur refuse de démarrer** (contrairement à Telegram,
ce n'est pas optionnel). Ne la perds jamais et ne la commite jamais : sans
elle, les données déjà chiffrées deviennent illisibles pour de bon, y
compris pour toi.

## Masquage des colonnes sensibles

À la définition du schéma, le client peut marquer une colonne comme
"sensible" (email, nom, téléphone...). Cette colonne n'apparaît alors JAMAIS
côté worker (ni brute, ni via un badge d'anomalie) -- seule une normalisation
automatique légère (espaces) lui est appliquée, sans relecture humaine. Les
colonnes non marquées restent corrigées normalement par les workers.

Compromis assumé : une faute fine dans une colonne sensible (ex: une lettre
inversée dans un nom) ne sera jamais corrigée, faute d'y avoir accès --
c'est le prix de la confidentialité sur ces colonnes.

## Validation manuelle des comptes

Un compte (client ou worker) fraîchement créé ne peut rien faire d'autre que
se connecter -- toute action fonctionnelle (déposer un fichier, prendre une
tâche...) est bloquée tant qu'il n'a pas été approuvé manuellement. Pas
d'interface web pour ça pour l'instant, un script suffit :

```bash
python3 admin_tools.py --lister              # comptes en attente
python3 admin_tools.py --approuver email@x.fr
```

## Notifications Telegram (optionnel)

Quand un client dépose un nouveau fichier, un message peut être envoyé
automatiquement dans un groupe Telegram pour prévenir les étudiants qu'il y a
du travail disponible.

1. Crée un bot via [@BotFather](https://t.me/BotFather) sur Telegram (`/newbot`) -- il te donne un token
2. Crée un groupe Telegram, ajoute le bot dedans
3. Envoie un message dans le groupe, puis va sur `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` dans ton navigateur pour trouver le `chat_id` (champ `"chat":{"id": ...}`)
4. Renseigne `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` dans le même fichier `.env`

Sans configuration, les notifications sont simplement désactivées -- le
reste de la plateforme fonctionne normalement.

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
| `crypto.py` | Chiffrement au repos des données clients |
| `notifications.py` | Alertes Telegram lors d'un nouveau dépôt |
| `api.py` | Endpoints FastAPI (auth, dépôt client, distribution des tâches) |
| `client_upload.html` | Interface entreprise : dépôt de CSV |
| `worker_dashboard.html` | Interface étudiant : traitement des tâches |
| `tests/` | Suite de tests `pytest` |

## Statut

Prototype fonctionnel, testé de bout en bout. Restent hors périmètre :
paiement réel (Stripe Connect ou équivalent), tableau de bord admin pour
les décaissements, tâche planifiée de nettoyage des verrous expirés
(actuellement nettoyés à la demande).
