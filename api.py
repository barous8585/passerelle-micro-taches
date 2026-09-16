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
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Column, DateTime, ForeignKey, Integer
from sqlalchemy.exc import IntegrityError

from analyzer import infer_schema, process_csv_to_microtasks, valider_consensus
from auth import get_current_user, hash_password, init_auth_db, require_role
from notifications import notifier_nouveau_projet
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
init_auth_db(SessionLocal)  # partage le même moteur/session avec auth.py -- voir auth.py


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
    accepte_confidentialite: bool = False


# Restreint l'inscription worker à des domaines email universitaires connus.
# Ne garantit pas qu'une personne ne crée pas plusieurs comptes (elle peut
# avoir plusieurs adresses @uco.fr), mais ferme la faille la plus grossière :
# n'importe qui créant 50 comptes gratuits en 5 minutes avec des emails
# jetables pour fausser le consensus. À étendre au fil des partenariats avec
# d'autres établissements (ex: "univ-angers.fr").
DOMAINES_WORKER_AUTORISES = ["uco.fr"]


def _domaine_email(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


@app.post("/auth/register")
def register(payload: RegisterIn):
    if payload.role not in ("client", "worker"):
        raise HTTPException(status_code=400, detail="Rôle invalide (client ou worker)")

    if payload.role == "worker":
        # Un worker manipule des données appartenant à des tiers (clients de nos
        # clients) -- l'engagement de confidentialité est une condition d'inscription,
        # pas une case facultative.
        if not payload.accepte_confidentialite:
            raise HTTPException(
                status_code=400,
                detail="Tu dois accepter l'engagement de confidentialité pour créer un compte étudiant.",
            )
        if _domaine_email(payload.email) not in DOMAINES_WORKER_AUTORISES:
            raise HTTPException(
                status_code=400,
                detail=f"Inscription étudiante réservée aux emails universitaires ({', '.join(DOMAINES_WORKER_AUTORISES)}).",
            )

    db = SessionLocal()
    try:
        if db.query(User).filter_by(email=payload.email).first():
            raise HTTPException(status_code=400, detail="Cet email est déjà utilisé")
        user = User(
            email=payload.email,
            role=RoleEnum(payload.role),
            password_hash=hash_password(payload.password),
            accepte_confidentialite=payload.accepte_confidentialite,
            date_acceptation_confidentialite=datetime.datetime.utcnow() if payload.accepte_confidentialite else None,
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
    # Borné pour éviter qu'un worker (malveillant ou par erreur de copier-coller)
    # ne soumette une chaîne de plusieurs Mo comme "correction" d'une seule
    # cellule, ou une liste de colonnes disproportionnée -- worker_id vient
    # de l'auth, jamais du payload.
    reponse: list[Annotated[str, StringConstraints(max_length=500)]] = Field(..., max_length=50)


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

        if len(payload.reponse) != len(task.header):
            raise HTTPException(
                status_code=400,
                detail=f"{len(task.header)} valeur(s) attendue(s) (une par colonne), {len(payload.reponse)} reçue(s).",
            )

        # Empêche un worker de soumettre plusieurs fois sur la même tâche
        # (auparavant : chaque re-soumission retraitait ET repayait toutes
        # les soumissions précédentes -- faille exploitable, corrigée ici).
        if db.query(Submission).filter_by(micro_task_id=task_id, worker_id=worker_id).first():
            raise HTTPException(status_code=409, detail="Tu as déjà soumis une réponse pour cette tâche.")

        db.query(TaskLock).filter_by(micro_task_id=task_id, worker_id=worker_id).delete()
        db.add(Submission(micro_task_id=task_id, worker_id=worker_id, reponse=payload.reponse))
        try:
            db.commit()
        except IntegrityError:
            # Deux requêtes quasi simultanées ont toutes deux passé le
            # contrôle ci-dessus -- la contrainte unique en base tranche.
            db.rollback()
            raise HTTPException(status_code=409, detail="Tu as déjà soumis une réponse pour cette tâche.")

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
            # La réponse de référence du gold standard EST la valeur propre connue
            # -- on la restitue telle quelle au client, peu importe ce que le worker a tapé.
            task.resultat_final = task.gold_answer
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
        task.resultat_final = construire_resultat_final(task.header, submissions[0].reponse, consensus)

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


def construire_resultat_final(header, premiere_reponse, consensus):
    """
    Reconstruit la ligne finale à restituer au client : la valeur consensuelle
    par colonne quand il y en a une, sinon on retombe sur la première
    soumission reçue (arrive seulement si une colonne du header n'est pas
    couverte par le schéma du projet -- cas limite).
    """
    if consensus["status"] == "auto_valide":
        return consensus["valeur_retenue"]
    detail = consensus["detail"]
    resultat = list(premiere_reponse)
    for i, nom_colonne in enumerate(header):
        if nom_colonne in detail:
            resultat[i] = detail[nom_colonne]["valeur"] if detail[nom_colonne]["status"] == "accord" \
                else detail[nom_colonne]["candidats"][0]  # litige : on restitue une valeur plausible, à revoir manuellement
    return resultat


SEUIL_DECAISSEMENT = 10.0  # seuil minimum avant qu'un versement groupé ait du sens


@app.get("/projects/available")
def projets_disponibles(user: User = Depends(require_role("worker"))):
    """
    Remplace la saisie manuelle d'un project_id : liste les projets ayant
    encore au moins une tâche que CE worker peut prendre (ni déjà traitée,
    ni déjà verrouillée par lui, ni au quota de redondance atteint).
    """
    db = SessionLocal()
    try:
        nettoyer_verrous_expires(db)
        deja_traitees = {s.micro_task_id for s in db.query(Submission).filter_by(worker_id=user.id)}
        deja_verrouillees = {l.micro_task_id for l in db.query(TaskLock).filter_by(worker_id=user.id)}

        resultat = []
        for projet in db.query(Project).all():
            nb_dispo = 0
            for tache in db.query(MicroTask).filter_by(project_id=projet.id, status=TaskStatus.available):
                if tache.id in deja_traitees or tache.id in deja_verrouillees:
                    continue
                nb_sub = db.query(Submission).filter_by(micro_task_id=tache.id).count()
                nb_lock = db.query(TaskLock).filter_by(micro_task_id=tache.id).count()
                if nb_sub + nb_lock < tache.redundancy_level:
                    nb_dispo += 1
            if nb_dispo > 0:
                resultat.append({
                    "project_id": projet.id,
                    "titre": projet.titre,
                    "prix_par_ligne": projet.prix_par_ligne,
                    "taches_disponibles": nb_dispo,
                })
        return {"projets": resultat}
    finally:
        db.close()


@app.post("/tasks/{task_id}/release")
def liberer_tache(task_id: int, user: User = Depends(require_role("worker"))):
    """Relâche volontairement le verrou avant les 15 minutes d'expiration --
    utile quand un worker ouvre une ligne puis décide de ne pas la traiter,
    pour ne pas geler inutilement un créneau de redondance."""
    db = SessionLocal()
    try:
        supprime = db.query(TaskLock).filter_by(micro_task_id=task_id, worker_id=user.id).delete()
        db.commit()
        if not supprime:
            raise HTTPException(status_code=404, detail="Aucun verrou actif sur cette tâche pour ce compte")
        return {"status": "liberee"}
    finally:
        db.close()


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
    """Sauvegarde le fichier uploadé, et le convertit en CSV s'il s'agit d'un
    Excel -- tout le reste du pipeline (inférence de schéma, découpage) ne
    connaît que le CSV, pas la peine de le dupliquer pour du xlsx."""
    suffixe = os.path.splitext(fichier.filename or "")[1].lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffixe or ".csv")
    with tmp as out:
        shutil.copyfileobj(fichier.file, out)
    chemin = tmp.name

    if suffixe in (".xlsx", ".xlsm"):
        chemin_csv = _convertir_excel_vers_csv(chemin)
        os.remove(chemin)
        return chemin_csv
    return chemin


def _convertir_excel_vers_csv(chemin_xlsx: str) -> str:
    """Lit la première feuille d'un classeur Excel et la réécrit en CSV.
    Le format .xls (ancien, pré-2007) n'est volontairement pas supporté --
    openpyxl ne le lit pas, et c'est un format en voie de disparition."""
    import csv as csv_module
    import openpyxl

    classeur = openpyxl.load_workbook(chemin_xlsx, data_only=True, read_only=True)
    feuille = classeur.active

    chemin_csv = tempfile.NamedTemporaryFile(delete=False, suffix=".csv").name
    with open(chemin_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv_module.writer(f, delimiter=";")
        for ligne in feuille.iter_rows(values_only=True):
            writer.writerow(["" if v is None else v for v in ligne])
    return chemin_csv


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

        notifier_nouveau_projet(projet.titre, len(tasks), projet.prix_par_ligne)

        return {"nb_taches_creees": len(tasks)}
    finally:
        db.close()


DELAI_PURGE_JOURS = 30  # rétention des données brutes après le dernier export du client


def purger_donnees_expirees(db):
    """Vide raw_data et resultat_final des tâches complétées dont le projet
    a été exporté il y a plus de DELAI_PURGE_JOURS -- on garde le statut et
    les métadonnées (utiles au trust_score et aux stats), mais plus le
    contenu métier une fois que le client a récupéré son résultat et que la
    fenêtre de rétention est dépassée. Appelée à la volée (même logique que
    nettoyer_verrous_expires), pas de tâche planifiée séparée pour ce MVP."""
    seuil = datetime.datetime.utcnow() - datetime.timedelta(days=DELAI_PURGE_JOURS)
    projets_a_purger = db.query(Project).filter(Project.date_dernier_export < seuil).all()
    for projet in projets_a_purger:
        db.query(MicroTask).filter_by(project_id=projet.id, status=TaskStatus.completed).update(
            {"raw_data": None, "resultat_final": None}
        )
    if projets_a_purger:
        db.commit()


@app.get("/projects/mine")
def mes_projets(user: User = Depends(require_role("client"))):
    """Vue d'ensemble pour le client : avancement de chacun de ses projets."""
    db = SessionLocal()
    try:
        purger_donnees_expirees(db)
        projets = db.query(Project).filter_by(client_id=user.id).all()
        resultat = []
        for p in projets:
            taches = db.query(MicroTask).filter_by(project_id=p.id).all()
            total = len(taches)
            completees = sum(1 for t in taches if t.status == TaskStatus.completed)
            litigieuses = sum(1 for t in taches if t.eu_litige)
            resultat.append({
                "project_id": p.id,
                "titre": p.titre,
                "total_lignes": total,
                "lignes_completees": completees,
                "lignes_litigieuses": litigieuses,
                "pret_pour_export": total > 0 and completees == total,
            })
        return {"projets": resultat}
    finally:
        db.close()


@app.get("/projects/{project_id}/export")
def exporter_projet(project_id: int, format: str = "csv", user: User = Depends(require_role("client"))):
    """Renvoie les données nettoyées -- uniquement les lignes déjà complétées.
    format=csv (défaut) ou format=xlsx. Les lignes encore en cours n'apparaissent
    pas (mieux vaut un export partiel explicite qu'une ligne vide ou brute qui
    passerait pour propre)."""
    if format not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format doit être 'csv' ou 'xlsx'")

    db = SessionLocal()
    try:
        purger_donnees_expirees(db)
        projet = db.query(Project).get(project_id)
        if not projet:
            raise HTTPException(status_code=404, detail="Projet introuvable")
        if projet.client_id != user.id:
            raise HTTPException(status_code=403, detail="Ce projet ne t'appartient pas")

        taches = (
            db.query(MicroTask)
            .filter_by(project_id=project_id, status=TaskStatus.completed)
            .order_by(MicroTask.row_id)
            .all()
        )
        # Les tâches dont les données ont été purgées (rétention dépassée)
        # n'ont plus rien à exporter -- on les exclut plutôt que de planter.
        taches = [t for t in taches if t.resultat_final is not None or t.raw_data is not None]
        if not taches:
            raise HTTPException(
                status_code=404,
                detail="Aucune ligne exportable (rien de complété, ou données purgées après le délai de rétention)",
            )

        projet.date_dernier_export = datetime.datetime.utcnow()
        db.commit()

        from fastapi.responses import Response

        if format == "xlsx":
            import io as io_module

            import openpyxl

            classeur = openpyxl.Workbook()
            feuille = classeur.active
            feuille.append(taches[0].header)
            for t in taches:
                feuille.append(t.resultat_final or t.raw_data)
            buffer = io_module.BytesIO()
            classeur.save(buffer)
            return Response(
                content=buffer.getvalue(),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f'attachment; filename="projet_{project_id}_nettoye.xlsx"'},
            )

        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(taches[0].header)
        for t in taches:
            writer.writerow(t.resultat_final or t.raw_data)
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="projet_{project_id}_nettoye.csv"'},
        )
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
