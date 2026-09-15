"""
Tests d'intégration -- reprennent les scénarios vérifiés manuellement
pendant le développement (voir historique du projet) sous forme de suite
automatisée. Utilise une vraie base SQLite de test (voir conftest.py) plutôt
que des mocks : le comportement du verrouillage anti-doublon et du consensus
dépend de l'état réel en base.
"""

import base64
import os

from fastapi.testclient import TestClient

from api import app

client = TestClient(app)
RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_TEST = os.path.join(RACINE, "donnees_brutes_test.csv")

SCHEMA_TEST = {
    "columns": [
        {"name": "nom", "type": "string", "required": True},
        {"name": "email", "type": "regex", "rule": r"^[\w.-]+@[\w.-]+\.\w+$", "required": True},
        {"name": "date_naiss", "type": "date", "required": True},
    ]
}


def auth_header(email, password):
    jeton = base64.b64encode(f"{email}:{password}".encode()).decode()
    return {"Authorization": f"Basic {jeton}"}


# ---------------------------------------------------------------------------
# AUTHENTIFICATION
# ---------------------------------------------------------------------------

def test_inscription_client_et_worker():
    r = client.post("/auth/register", json={"email": "startup@ia.fr", "password": "pw123", "role": "client"})
    assert r.status_code == 200
    r = client.post("/auth/register", json={"email": "w1@uco.fr", "password": "pw123", "role": "worker"})
    assert r.status_code == 200
    r = client.post("/auth/register", json={"email": "w2@uco.fr", "password": "pw123", "role": "worker"})
    assert r.status_code == 200


def test_inscription_email_deja_utilise_refusee():
    r = client.post("/auth/register", json={"email": "startup@ia.fr", "password": "autre", "role": "client"})
    assert r.status_code == 400


def test_connexion_mauvais_mot_de_passe_refusee():
    r = client.get("/auth/me", headers=auth_header("startup@ia.fr", "mauvais_mdp"))
    assert r.status_code == 401


def test_worker_ne_peut_pas_creer_de_projet():
    r = client.post(
        "/projects", json={"titre": "x", "colonnes_schema": {"columns": []}},
        headers=auth_header("w1@uco.fr", "pw123"),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# FLUX CLIENT : inférence de schéma, création de projet, ingestion
# ---------------------------------------------------------------------------

def test_inference_schema_detecte_les_bons_types():
    with open(CSV_TEST, "rb") as f:
        r = client.post(
            "/projects/infer-schema", files={"fichier": ("d.csv", f, "text/csv")},
            headers=auth_header("startup@ia.fr", "pw123"),
        )
    assert r.status_code == 200
    types_par_nom = {c["name"]: c["type"] for c in r.json()["schema_suggere"]["columns"]}
    assert types_par_nom["email"] == "regex"
    assert types_par_nom["date_naiss"] == "date"


def test_creation_projet_et_ingestion_csv():
    r = client.post(
        "/projects",
        json={"titre": "Test intégration", "colonnes_schema": SCHEMA_TEST, "prix_par_ligne": 0.05},
        headers=auth_header("startup@ia.fr", "pw123"),
    )
    assert r.status_code == 200
    project_id = r.json()["project_id"]

    with open(CSV_TEST, "rb") as f:
        r = client.post(
            f"/projects/{project_id}/ingest", files={"fichier": ("d.csv", f, "text/csv")},
            headers=auth_header("startup@ia.fr", "pw123"),
        )
    assert r.status_code == 200
    assert r.json()["nb_taches_creees"] == 4

    # Un client ne peut pas ingérer sur le projet d'un autre
    r2 = client.post("/auth/register", json={"email": "autre_client@ia.fr", "password": "pw123", "role": "client"})
    with open(CSV_TEST, "rb") as f:
        r = client.post(
            f"/projects/{project_id}/ingest", files={"fichier": ("d.csv", f, "text/csv")},
            headers=auth_header("autre_client@ia.fr", "pw123"),
        )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# DISTRIBUTION ET VERROUILLAGE ANTI-DOUBLON
# ---------------------------------------------------------------------------

def creer_projet_isole(nom_projet, client_email="startup@ia.fr"):
    """Crée un projet + l'ingère avec le CSV de test -> donne à un test un lot
    de micro-tâches totalement frais, sans verrou ni soumission héritée d'un
    autre test. Évite le couplage d'état entre tests."""
    r = client.post(
        "/projects",
        json={"titre": nom_projet, "colonnes_schema": SCHEMA_TEST, "prix_par_ligne": 0.05},
        headers=auth_header(client_email, "pw123"),
    )
    project_id = r.json()["project_id"]
    with open(CSV_TEST, "rb") as f:
        client.post(
            f"/projects/{project_id}/ingest", files={"fichier": ("d.csv", f, "text/csv")},
            headers=auth_header(client_email, "pw123"),
        )
    return project_id


# ---------------------------------------------------------------------------
# DISTRIBUTION ET VERROUILLAGE ANTI-DOUBLON
# ---------------------------------------------------------------------------

def test_deux_workers_ne_recoivent_jamais_la_meme_ligne_propre():
    project_id = creer_projet_isole("Test verrouillage")
    r1 = client.get("/tasks/next", params={"project_id": project_id}, headers=auth_header("w1@uco.fr", "pw123"))
    r2 = client.get("/tasks/next", params={"project_id": project_id}, headers=auth_header("w2@uco.fr", "pw123"))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["task_id"] != r2.json()["task_id"]


def test_worker_ne_peut_pas_soumettre_sans_authentification():
    r = client.post("/tasks/1/submit", json={"reponse": ["a", "b", "c"]})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# CONSENSUS, PAIEMENT SYSTÉMATIQUE ET AJUSTEMENT DU TRUST_SCORE
# ---------------------------------------------------------------------------

def test_ligne_propre_redondance_1_payee_immediatement():
    project_id = creer_projet_isole("Test paiement immediat")
    r = client.get("/tasks/next", params={"project_id": project_id}, headers=auth_header("w1@uco.fr", "pw123"))
    tache = r.json()
    assert tache["row_id"] == 0  # la ligne propre du CSV de test, toujours servie en premier

    r = client.post(
        f"/tasks/{tache['task_id']}/submit", json={"reponse": tache["raw_data"]},
        headers=auth_header("w1@uco.fr", "pw123"),
    )
    assert r.json()["paye"] is True


def test_litige_est_paye_mais_impacte_le_trust_score():
    from models import MicroTask, get_engine, get_session_factory

    project_id = creer_projet_isole("Test litige")
    db = get_session_factory(get_engine())()
    tache = db.query(MicroTask).filter_by(row_id=3, project_id=project_id).first()
    tache.redundancy_level = 2
    db.commit()
    task_id = tache.id
    db.close()

    trust_avant_w1 = client.get("/auth/me", headers=auth_header("w1@uco.fr", "pw123")).json()["trust_score"]
    trust_avant_w2 = client.get("/auth/me", headers=auth_header("w2@uco.fr", "pw123")).json()["trust_score"]

    r1 = client.post(
        f"/tasks/{task_id}/submit",
        json={"reponse": ["Sophie Legrand", "sophie@legrand.fr", "1990-11-30"]},
        headers=auth_header("w1@uco.fr", "pw123"),
    )
    assert r1.json()["status"] == "en_attente_autres_workers"
    assert r1.json()["paye"] is None

    r2 = client.post(
        f"/tasks/{task_id}/submit",
        json={"reponse": ["Quelqu'un d'autre", "faux@x.fr", "2000-01-01"]},
        headers=auth_header("w2@uco.fr", "pw123"),
    )
    # Décision produit : payé même en cas de désaccord
    assert r2.json()["paye"] is True

    trust_apres_w1 = client.get("/auth/me", headers=auth_header("w1@uco.fr", "pw123")).json()["trust_score"]
    trust_apres_w2 = client.get("/auth/me", headers=auth_header("w2@uco.fr", "pw123")).json()["trust_score"]

    assert trust_apres_w1 >= trust_avant_w1   # w1 rejoint le consensus -> stable ou en hausse
    assert trust_apres_w2 < trust_avant_w2    # w2 diverge nettement -> pénalisé


# ---------------------------------------------------------------------------
# HISTORIQUE WORKER : pas de fuite sur les gold standards / litiges
# ---------------------------------------------------------------------------

def test_historique_worker_ne_contient_aucun_texte_de_reponse():
    r = client.get("/tasks/mine", headers=auth_header("w1@uco.fr", "pw123"))
    assert r.status_code == 200
    data = r.json()
    assert "solde_disponible" in data
    for ligne in data["historique"]:
        assert set(ligne.keys()) == {"task_id", "date", "statut", "montant"}
        assert ligne["statut"] in ("paye", "en_attente_verification")
