"""
api.py — API de service des micro-tâches.

Règles métier tranchées (voir échanges produit) :
  - Un litige (désaccord entre workers hors gold standard) N'EMPÊCHE PAS le
    paiement -> on ne veut pas démotiver un worker de bonne foi qui ne
    contrôle pas la qualité des autres. Seul le trust_score est ajusté,
    proportionnellement à l'écart avec le consensus pondéré.
  - Les gold standards restent invisibles au worker (sinon il change de
    comportement dessus et fausse la mesure) -> seul un indicateur agrégé
    (trust_score) est exposé via /auth/me.
  - Un worker dont le trust_score tombe sous un seuil ne pèse plus dans le
    calcul du consensus (mais reste payé et continue de recevoir des tâches).

Deux endpoints critiques restent au cœur du système :
  GET  /tasks/next        -> sert une tâche au worker authentifié, avec verrou temporaire
  POST /tasks/{id}/submit -> enregistre une réponse, déclenche le consensus
                             une fois le quota de redondance atteint
"""

import datetime
import os
import shutil
import tempfile

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import Column, DateTime, ForeignKey, Integer

from analyzer import infer_schema, process_csv_to_microtasks, valider_consensus
from auth import get_current_user, hash_password, require_role
from models import (Base, MicroTask, Project, RoleEnum, Submission,
                     TaskStatus, User, get_engine, get_session_factory)

SEUIL_TRUST_BAISSE = 0.4   # écart moyen <= ce seuil -> le worker a divergé du consensus
SEUIL_TRUST_HAUSSE = 0.8   # écart moyen >= ce seuil -> le worker a bien suivi le consensus


class TaskLock(Base):
    """Verrou temporaire : un worker qui a récupéré une tâche mais n'a pas
    encore soumis. Expire après LOCK_TIMEOUT_MINUTES pour ne pas bloquer
    une ligne si l'étudiant ferme son onglet sans valider."""
    __tablename__ = "task_locks"
    id = Column(Integer, primary_key=True)
    micro_task_id = Column(Integer, ForeignKey("micro_tasks.id"))
    worker_id = Column(Integer, ForeignKey("users.id"))
    locked_at = Column(DateTime, default=datetime.datetime.utcnow)


LOCK_TIMEOUT_MINUTES = 15

app = FastAPI(title="Passerelle de Micro-Tâches Data")
engine = get_engine()
SessionLocal = get_session_factory(engine)  # crée toutes les tables, y compris task_locks


def nettoyer_verrous_expires(db):
    seuil = datetime.datetime.utcnow() - datetime.timedelta(minutes=LOCK_TIMEOUT_MINUTES)
    db.query(TaskLock).filter(TaskLock.locked_at < seuil).delete()
    db.commit()


# ---------------------------------------------------------------------------
# AUTHENTIFICATION
# ---------------------------------------------------------------------------

class RegisterIn(BaseModel):
    email: str
    password: str
    role: str  # "client" ou "worker" -- le rôle "admin" ne se crée pas via l'API


@app.post("/auth/register")
def register(payload: RegisterIn):
    if payload.role not in ("client", "worker"):
        raise HTTPException(status_code=400, detail="Rôle invalide (client ou worker)")
    db = SessionLocal()
    try:
        if db.query(User).filter_by(email=payload.email).first():
            raise HTTPException(status_code=400, detail="Cet email est déjà utilisé")
        user = User(
            email=payload.email,
            role=RoleEnum(payload.role),
            password_hash=hash_password(payload.password),
        )
        db.add(user)
        db.commit()
        return {"user_id": user.id, "role": user.role.value}
    finally:
        db.close()


@app.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    """Utilisé par le front pour vérifier la connexion ET afficher un
    indicateur de performance agrégé au worker -- sans jamais révéler quelles
    lignes précises étaient des gold standards."""
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role.value,
        "trust_score": round(user.trust_score, 1),
        "tasks_completed": user.tasks_completed,
    }


class SubmissionIn(BaseModel):
    reponse: list[str]  # la ligne corrigée, colonne par colonne (worker_id vient de l'auth, plus du payload)


@app.get("/tasks/next")
def get_next_task(project_id: int, user: User = Depends(require_role("worker"))):
    db = SessionLocal()
    try:
        nettoyer_verrous_expires(db)
        worker_id = user.id

        deja_traitees = {
            s.micro_task_id for s in db.query(Submission).filter_by(worker_id=worker_id)
        }
        deja_verrouillees_par_moi = {
            l.micro_task_id for l in db.query(TaskLock).filter_by(worker_id=worker_id)
        }

        candidats = (
            db.query(MicroTask)
            .filter(MicroTask.project_id == project_id, MicroTask.status == TaskStatus.available)
            .all()
        )

        for task in candidats:
            if task.id in deja_traitees or task.id in deja_verrouillees_par_moi:
                continue  # ce worker a déjà traité ou verrouille déjà cette ligne

            nb_soumissions = db.query(Submission).filter_by(micro_task_id=task.id).count()
            nb_verrous_actifs = db.query(TaskLock).filter_by(micro_task_id=task.id).count()

            if nb_soumissions + nb_verrous_actifs < task.redundancy_level:
                db.add(TaskLock(micro_task_id=task.id, worker_id=worker_id))
                db.commit()
                return {
                    "task_id": task.id,
                    "row_id": task.row_id,
                    "raw_data": task.raw_data,
                    "header": task.header,
                    "badges": task.badges,
                    "prix": db.query(Project).get(project_id).prix_par_ligne,
                }

        raise HTTPException(status_code=404, detail="Aucune tâche disponible pour le moment")
    finally:
        db.close()


def _ajuster_trust_score(worker: User, ecart: float):
    """ecart = proportion de colonnes où ce worker rejoint le consensus pondéré.
    Ajustement volontairement doux (le paiement, lui, n'est jamais affecté)."""
    if ecart >= SEUIL_TRUST_HAUSSE:
        worker.trust_score = min(100.0, worker.trust_score + 0.5)
    elif ecart <= SEUIL_TRUST_BAISSE:
        worker.trust_score = max(0.0, worker.trust_score - 3.0)
    # entre les deux seuils : léger désaccord toléré, pas d'ajustement


@app.post("/tasks/{task_id}/submit")
def submit_task(task_id: int, payload: SubmissionIn, user: User = Depends(require_role("worker"))):
    db = SessionLocal()
    try:
        task = db.query(MicroTask).get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Tâche introuvable")
        worker_id = user.id

        db.query(TaskLock).filter_by(micro_task_id=task_id, worker_id=worker_id).delete()
        db.add(Submission(micro_task_id=task_id, worker_id=worker_id, reponse=payload.reponse))
        db.commit()

        submissions = db.query(Submission).filter_by(micro_task_id=task_id).all()

        # --- Cas gold standard : seul cas où le trust_score bouge FORTEMENT,
        # car c'est le seul signal 100% fiable (on connaît la vraie réponse).
        # Le worker est payé dans tous les cas -- gold standard = contrôle
        # qualité invisible, pas une pénalité de rémunération. ---
        if task.is_gold_standard:
            correct = payload.reponse == task.gold_answer
            worker = db.query(User).get(worker_id)
            worker.trust_score = min(100.0, worker.trust_score + 1) if correct \
                else max(0.0, worker.trust_score - 5)
            worker.tasks_completed += 1
            prix = db.query(Project).get(task.project_id).prix_par_ligne
            worker.solde_disponible += prix
            db.query(Submission).filter_by(micro_task_id=task_id, worker_id=worker_id).update({"montant": prix})
            task.status = TaskStatus.completed
            db.commit()
            return {"status": "completed", "paye": True}

        # --- Cas normal : on attend d'avoir atteint le niveau de redondance ---
        if len(submissions) < task.redundancy_level:
            db.commit()
            return {"status": "en_attente_autres_workers", "soumissions_recues": len(submissions), "paye": None}

        workers = [db.query(User).get(s.worker_id) for s in submissions]
        consensus = valider_consensus(
            [s.reponse for s in submissions], task.schema_json, task.header,
            trust_scores=[w.trust_score for w in workers],
        )

        # Décision produit : toujours payer hors gold standard, qu'il y ait
        # litige ou non -- seul le trust_score encaisse l'écart au consensus.
        task.eu_litige = consensus["status"] not in ("valide", "auto_valide")
        task.status = TaskStatus.completed

        prix = db.query(Project).get(task.project_id).prix_par_ligne
        for worker, sub, ecart in zip(workers, submissions, consensus["ecarts_par_worker"]):
            _ajuster_trust_score(worker, ecart)
            worker.tasks_completed += 1
            worker.solde_disponible += prix
            sub.montant = prix
        db.commit()

        return {"status": "completed", "paye": True}
    finally:
        db.close()


SEUIL_DECAISSEMENT = 10.0  # seuil minimum avant qu'un versement groupé ait du sens


@app.get("/tasks/mine")
def mes_taches(user: User = Depends(require_role("worker"))):
    """
    Historique du worker connecté -- VOLONTAIREMENT limité au statut et au
    montant. Pas de texte de réponse, pas d'indicateur gold standard, pas de
    détail du consensus : un worker curieux ne doit rien pouvoir en déduire
    sur le fonctionnement interne du contrôle qualité.
    """
    db = SessionLocal()
    try:
        subs = (
            db.query(Submission)
            .filter_by(worker_id=user.id)
            .order_by(Submission.created_at.desc())
            .all()
        )
        historique = [
            {
                "task_id": s.micro_task_id,
                "date": s.created_at.isoformat(),
                "statut": "paye" if s.montant is not None else "en_attente_verification",
                "montant": s.montant,
            }
            for s in subs[:50]  # les 50 plus récentes -- pas de pagination pour le MVP
        ]
        return {
            "solde_disponible": round(user.solde_disponible, 2),
            "seuil_decaissement": SEUIL_DECAISSEMENT,
            "eligible_versement": user.solde_disponible >= SEUIL_DECAISSEMENT,
            "nb_taches_payees": sum(1 for s in subs if s.montant is not None),
            "nb_en_attente": sum(1 for s in subs if s.montant is None),
            "historique": historique,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ENDPOINTS CÔTÉ CLIENT : dépôt de CSV + inférence de schéma
# ---------------------------------------------------------------------------

def _sauver_upload_temporaire(fichier: UploadFile) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    with tmp as out:
        shutil.copyfileobj(fichier.file, out)
    return tmp.name


@app.post("/projects/infer-schema")
def preview_schema(fichier: UploadFile = File(...), user: User = Depends(require_role("client"))):
    chemin = _sauver_upload_temporaire(fichier)
    try:
        schema, header, colonnes_valeurs = infer_schema(chemin)
        apercu = [
            dict(zip(header, valeurs))
            for valeurs in zip(*colonnes_valeurs[:5]) if colonnes_valeurs
        ] if colonnes_valeurs and colonnes_valeurs[0] else []
        return {"schema_suggere": schema, "header": header, "apercu_lignes": apercu[:5]}
    finally:
        os.remove(chemin)


class ProjectIn(BaseModel):
    titre: str
    colonnes_schema: dict
    prix_par_ligne: float = 0.04


@app.post("/projects")
def creer_projet(payload: ProjectIn, user: User = Depends(require_role("client"))):
    db = SessionLocal()
    try:
        projet = Project(
            client_id=user.id,
            titre=payload.titre,
            schema_json=payload.colonnes_schema,
            prix_par_ligne=payload.prix_par_ligne,
        )
        db.add(projet)
        db.commit()
        return {"project_id": projet.id}
    finally:
        db.close()


@app.post("/projects/{project_id}/ingest")
def ingest_csv(project_id: int, fichier: UploadFile = File(...), user: User = Depends(require_role("client"))):
    db = SessionLocal()
    try:
        projet = db.query(Project).get(project_id)
        if not projet:
            raise HTTPException(status_code=404, detail="Projet introuvable")
        if projet.client_id != user.id:
            raise HTTPException(status_code=403, detail="Ce projet ne t'appartient pas")

        chemin = _sauver_upload_temporaire(fichier)
        try:
            tasks = process_csv_to_microtasks(chemin, project_id, projet.schema_json)
        finally:
            os.remove(chemin)

        for t in tasks:
            db.add(MicroTask(**t))
        db.commit()
        return {"nb_taches_creees": len(tasks)}
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def page_depot_client():
    with open("client_upload.html", encoding="utf-8") as f:
        return f.read()


@app.get("/worker", response_class=HTMLResponse)
def page_worker_dashboard():
    with open("worker_dashboard.html", encoding="utf-8") as f:
        return f.read()
