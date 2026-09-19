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
import tempfile

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer
from sqlalchemy.exc import IntegrityError

from analyzer import (detect_anomalies, infer_schema, normaliser_automatiquement,
                       normaliser_espaces, process_csv_to_microtasks, valider_consensus)
from auth import (authentifier, creer_session, generer_code_liaison, generer_otp,
                   get_current_user, hash_password, init_auth_db, lier_via_code,
                   require_role, revoquer_session, security, verifier_otp)
from crypto import chiffrer_json, dechiffrer_json
from notifications import (TELEGRAM_BOT_TOKEN, envoyer_code_connexion,
                            envoyer_confirmation_liaison, notifier_nouveau_projet)
from models import (AccessLog, Base, MicroTask, Project, RoleEnum, Submission,
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


class Payout(Base):
    """Trace d'un versement marqué comme effectué par un admin -- le virement
    réel (Wave, Orange Money, etc.) se fait hors plateforme, cette table sert
    juste à garder un historique et remettre le solde à zéro."""
    __tablename__ = "payouts"
    id = Column(Integer, primary_key=True)
    worker_id = Column(Integer, ForeignKey("users.id"))
    montant = Column(Float, nullable=False)
    date_versement = Column(DateTime, default=datetime.datetime.utcnow)


LOCK_TIMEOUT_MINUTES = 15


def _journaliser_acces(db, user: User, action: str, project_id: int | None = None):
    """Consigne un événement significatif (connexion, export) dans le
    journal d'accès. Best-effort : une erreur ici ne doit jamais faire
    échouer l'action réelle de l'utilisateur, donc on avale l'exception."""
    try:
        db.add(AccessLog(
            user_id=user.id, email=user.email, role=user.role.value,
            action=action, project_id=project_id,
        ))
        db.commit()
    except Exception:
        db.rollback()


app = FastAPI(title="Passerelle de Micro-Tâches Data")

# CORS -- nécessaire dès que le frontend (Netlify) et le backend (Render) ne
# sont plus sur le même nom de domaine. FRONTEND_ORIGINS est une liste
# d'origines autorisées séparées par des virgules (ex. sur Render :
# "https://passerelle-data.netlify.app,https://tondomaine.fr"). En local,
# personne n'a besoin de CORS (même origine), donc la valeur par défaut
# n'autorise rien d'externe -- pas de risque à l'oublier en dev.
_ORIGINES_AUTORISEES = [
    o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ORIGINES_AUTORISEES,
    allow_credentials=False,  # l'auth passe par un header Authorization, pas des cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# DATABASE_URL -- fourni automatiquement par Render quand tu ajoutes une base
# PostgreSQL gérée au service. Absent (ex. en local) -> repli sur SQLite comme
# avant. Render fournit parfois une URL commençant par "postgres://" (ancien
# format) alors que SQLAlchemy 2.x exige "postgresql://" -- on corrige au vol.
_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./passerelle.db")
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = get_engine(_DATABASE_URL)
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
    secteur_activite: str | None = None  # obligatoire pour les clients, ignoré pour les workers


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

    if payload.role == "client" and not (payload.secteur_activite or "").strip():
        raise HTTPException(status_code=400, detail="Le secteur d'activité est obligatoire pour un compte entreprise.")

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
            approuve=False,  # validation manuelle requise avant tout accès fonctionnel -- voir admin_tools.py
            secteur_activite=payload.secteur_activite.strip() if payload.role == "client" else None,
        )
        db.add(user)
        db.commit()
        return {"user_id": user.id, "role": user.role.value, "approuve": user.approuve}
    finally:
        db.close()


class LoginIn(BaseModel):
    email: Annotated[str, StringConstraints(max_length=255)]
    password: Annotated[str, StringConstraints(max_length=200)]


@app.post("/auth/login")
def login(payload: LoginIn):
    """Vérifie email + mot de passe UNE SEULE FOIS et renvoie un jeton de
    session temporaire (voir auth.py) -- le front ne renverra plus jamais
    le mot de passe après ça, seulement ce jeton, qui expire tout seul."""
    db = SessionLocal()
    try:
        user = authentifier(db, payload.email, payload.password)
        jeton = creer_session(db, user)
        _journaliser_acces(db, user, "connexion")
        return {
            "token": jeton,
            "id": user.id,
            "email": user.email,
            "role": user.role.value,
            "approuve": user.approuve,
        }
    finally:
        db.close()


@app.post("/auth/logout")
def logout(credentials=Depends(security)):
    """Révoque le jeton immédiatement, sans attendre son expiration
    naturelle -- utile si l'appareil est partagé ou en cas de doute."""
    db = SessionLocal()
    try:
        revoquer_session(db, credentials.credentials)
        return {"status": "deconnecte"}
    finally:
        db.close()


@app.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    """Utilisé par le front pour vérifier que le jeton de session est
    toujours valide ET afficher un indicateur de performance agrégé au
    worker -- sans jamais révéler quelles lignes précises étaient des gold
    standards."""
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role.value,
        "approuve": user.approuve,
        "trust_score": round(user.trust_score, 1),
        "tasks_completed": user.tasks_completed,
        "telegram_lie": bool(user.telegram_chat_id),
    }


@app.post("/auth/telegram/generer-code")
def telegram_generer_code(user: User = Depends(get_current_user)):
    """Génère un code à coller dans le bot Telegram pour lier le compte --
    appelé depuis une session encore authentifiée par mot de passe, avant
    la liaison. Une fois liée, la connexion par mot de passe ne fonctionne
    plus (voir authentifier() dans auth.py)."""
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="Le bot Telegram n'est pas configuré côté serveur.")
    db = SessionLocal()
    try:
        # `user` vient de Depends(get_current_user), une session déjà fermée --
        # le mutable en place ne serait pas suivi par CETTE session. On
        # recharge la ligne dans `db` avant de la modifier et de committer.
        user_frais = db.query(User).filter_by(id=user.id).first()
        code = generer_code_liaison(db, user_frais)
        return {"code": code, "duree_minutes": 10}
    finally:
        db.close()


class OtpDemandeIn(BaseModel):
    email: Annotated[str, StringConstraints(max_length=255)]


@app.post("/auth/otp/demander")
def otp_demander(payload: OtpDemandeIn):
    """Déclenche l'envoi d'un code de connexion via Telegram. Renvoie
    toujours le même message générique, que le compte existe ou non et
    qu'il soit lié à Telegram ou non -- éviter de révéler si un email est
    inscrit sur la plateforme (énumération de comptes)."""
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=payload.email).first()
        if user and user.telegram_chat_id:
            code = generer_otp(db, user)
            envoyer_code_connexion(user.telegram_chat_id, code)
        return {"message": "Si ce compte existe et est lié à Telegram, un code vient d'être envoyé."}
    finally:
        db.close()


class OtpVerifierIn(BaseModel):
    email: Annotated[str, StringConstraints(max_length=255)]
    code: Annotated[str, StringConstraints(max_length=10)]


@app.post("/auth/otp/verifier")
def otp_verifier(payload: OtpVerifierIn):
    db = SessionLocal()
    try:
        user = verifier_otp(db, payload.email, payload.code)
        jeton = creer_session(db, user)
        _journaliser_acces(db, user, "connexion")
        return {
            "token": jeton,
            "id": user.id,
            "email": user.email,
            "role": user.role.value,
            "approuve": user.approuve,
        }
    finally:
        db.close()


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Reçoit les messages envoyés au bot Telegram par les utilisateurs.
    On ne traite QUE le cas d'un code de liaison valide -- tout le reste
    (spam, "/start" seul, texte quelconque) est silencieusement ignoré.
    Renvoie toujours 200 : Telegram réessaie indéfiniment sinon."""
    try:
        update = await request.json()
        message = update.get("message") or {}
        texte = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if texte and chat_id:
            db = SessionLocal()
            try:
                user = lier_via_code(db, texte, chat_id)
                if user:
                    envoyer_confirmation_liaison(str(chat_id))
            finally:
                db.close()
    except Exception as e:
        print(f"⚠️  Erreur webhook Telegram (ignorée) : {e}")
    return {"ok": True}


class SubmissionIn(BaseModel):
    # Borné pour éviter qu'un worker (malveillant ou par erreur de copier-coller)
    # ne soumette une chaîne de plusieurs Mo comme "correction" d'une seule
    # cellule, ou une liste de colonnes disproportionnée -- worker_id vient
    # de l'auth, jamais du payload.
    reponse: list[Annotated[str, StringConstraints(max_length=500)]] = Field(..., max_length=50)


def _noms_colonnes_sensibles(schema_json):
    return {col["name"] for col in schema_json.get("columns", []) if col.get("sensible")}


def _filtrer_pour_worker(header, raw_data, badges, schema_json):
    """Retire les colonnes marquées 'sensible' de ce qui est montré au worker
    -- il ne doit jamais voir la ligne complète et nominative du client sur
    ces colonnes-là. Les badges des colonnes retirées sont supprimés, les
    autres réindexés sur les positions filtrées."""
    sensibles = _noms_colonnes_sensibles(schema_json)
    indices_visibles = [i for i, nom in enumerate(header) if nom not in sensibles]

    header_visible = [header[i] for i in indices_visibles]
    raw_data_visible = [raw_data[i] for i in indices_visibles]

    ancien_vers_nouveau = {ancien: nouveau for nouveau, ancien in enumerate(indices_visibles)}
    badges_visibles = [
        {**b, "col_index": ancien_vers_nouveau[b["col_index"]]}
        for b in badges
        if b["col_index"] in ancien_vers_nouveau
    ]
    return header_visible, raw_data_visible, badges_visibles


def _reconstituer_reponse_complete(header, raw_data, reponse_visible, schema_json):
    """Inverse de _filtrer_pour_worker : recombine les corrections du worker
    (colonnes non sensibles) avec les valeurs brutes normalisées automatiquement
    (colonnes sensibles, jamais montrées ni éditées par le worker)."""
    sensibles = _noms_colonnes_sensibles(schema_json)
    reponse_complete = []
    index_visible = 0
    for i, nom in enumerate(header):
        if nom in sensibles:
            reponse_complete.append(normaliser_automatiquement(raw_data[i]))
        else:
            reponse_complete.append(reponse_visible[index_visible])
            index_visible += 1
    return reponse_complete


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
                header_visible, raw_data_visible, badges_visibles = _filtrer_pour_worker(
                    task.header, dechiffrer_json(task.raw_data), task.badges, task.schema_json
                )
                return {
                    "task_id": task.id,
                    "row_id": task.row_id,
                    "raw_data": raw_data_visible,
                    "header": header_visible,
                    "badges": badges_visibles,
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

        nb_colonnes_visibles = len(task.header) - len(
            _noms_colonnes_sensibles(task.schema_json) & set(task.header)
        )
        if len(payload.reponse) != nb_colonnes_visibles:
            raise HTTPException(
                status_code=400,
                detail=f"{nb_colonnes_visibles} valeur(s) attendue(s) (colonnes visibles uniquement), {len(payload.reponse)} reçue(s).",
            )

        # Recombine les corrections du worker (colonnes visibles) avec les
        # valeurs des colonnes sensibles -- jamais montrées ni éditées par
        # lui, juste normalisées automatiquement (espaces).
        reponse_complete = _reconstituer_reponse_complete(
            task.header, dechiffrer_json(task.raw_data), payload.reponse, task.schema_json
        )

        # Contrôle qualité bloquant : les badges affichés au worker (champ
        # requis vide, format invalide) ne servaient jusqu'ici que d'indice
        # visuel -- rien n'empêchait de soumettre quand même sans corriger.
        # On relance la même détection sur SA réponse et on refuse la
        # soumission si un problème bloquant subsiste sur une colonne QU'IL
        # PEUT VOIR ET CORRIGER -- jamais sur une colonne sensible, que le
        # worker ne voit ni ne modifie jamais (ça bloquerait sans issue).
        # Les simples avertissements (espaces) ne bloquent pas : ils sont
        # déjà corrigés silencieusement ci-dessous.
        badges_apres_soumission = detect_anomalies(reponse_complete, task.header, task.schema_json)
        _, _, badges_visibles_apres_soumission = _filtrer_pour_worker(
            task.header, reponse_complete, badges_apres_soumission, task.schema_json
        )
        bloquants_visibles = [
            b for b in badges_visibles_apres_soumission
            if b["type"] == "danger" or b["msg"] == "Champ vide/absent"
        ]
        if bloquants_visibles:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Certains champs ne sont pas encore corrects, corrige-les avant de valider.",
                    "colonnes": bloquants_visibles,
                },
            )

        # Les espaces superflus restants (colonnes non couvertes par la
        # normalisation automatique par type, ex. ville/code postal) sont
        # corrigés silencieusement plutôt que de pénaliser le worker pour ça.
        reponse_complete = [normaliser_espaces(v) for v in reponse_complete]

        # Empêche un worker de soumettre plusieurs fois sur la même tâche
        # (auparavant : chaque re-soumission retraitait ET repayait toutes
        # les soumissions précédentes -- faille exploitable, corrigée ici).
        if db.query(Submission).filter_by(micro_task_id=task_id, worker_id=worker_id).first():
            raise HTTPException(status_code=409, detail="Tu as déjà soumis une réponse pour cette tâche.")

        db.query(TaskLock).filter_by(micro_task_id=task_id, worker_id=worker_id).delete()
        db.add(Submission(micro_task_id=task_id, worker_id=worker_id, reponse=chiffrer_json(reponse_complete)))
        try:
            db.commit()
        except IntegrityError:
            # Deux requêtes quasi simultanées ont toutes deux passé le
            # contrôle ci-dessus -- la contrainte unique en base tranche.
            db.rollback()
            raise HTTPException(status_code=409, detail="Tu as déjà soumis une réponse pour cette tâche.")

        submissions = db.query(Submission).filter_by(micro_task_id=task_id).all()

        # --- Cas gold standard : sert à calibrer le trust_score d'un worker
        # sur des lignes dont on connaît déjà la réponse attendue.
        #
        # ATTENTION -- ici, "réponse attendue" vient à 100% du nettoyage
        # automatique lui-même (aucune vérité externe fournie), donc ce
        # n'est PAS une vérité absolue : si le worker n'est pas d'accord
        # avec elle, ça peut vouloir dire qu'il a repéré une vraie erreur
        # que la machine avait laissée passer. Avant, on jetait purement et
        # simplement sa correction et on gardait la version machine -- une
        # ligne fausse pouvait donc rester "correcte" pour toujours, sans
        # que personne ne s'en aperçoive. Maintenant, un désaccord est traité
        # comme un litige normal (voir "Projets & litiges" dans l'admin) : la
        # correction du worker est conservée et la ligne est signalée pour
        # arbitrage humain, au lieu d'être écrasée en silence.
        if task.is_gold_standard:
            reponse_reference = dechiffrer_json(task.gold_answer)
            correct = reponse_complete == reponse_reference
            worker = db.query(User).get(worker_id)
            worker.trust_score = min(100.0, worker.trust_score + 1) if correct \
                else max(0.0, worker.trust_score - 5)
            worker.tasks_completed += 1
            prix = db.query(Project).get(task.project_id).prix_par_ligne
            worker.solde_disponible += prix
            db.query(Submission).filter_by(micro_task_id=task_id, worker_id=worker_id).update({"montant": prix})
            task.status = TaskStatus.completed
            if correct:
                task.resultat_final = task.gold_answer
                task.eu_litige = False
            else:
                task.resultat_final = chiffrer_json(reponse_complete)
                task.eu_litige = True
            db.commit()
            return {"status": "completed", "paye": True}

        # --- Cas normal : on attend d'avoir atteint le niveau de redondance ---
        if len(submissions) < task.redundancy_level:
            db.commit()
            return {"status": "en_attente_autres_workers", "soumissions_recues": len(submissions), "paye": None}

        workers = [db.query(User).get(s.worker_id) for s in submissions]
        reponses_dechiffrees = [dechiffrer_json(s.reponse) for s in submissions]
        consensus = valider_consensus(
            reponses_dechiffrees, task.schema_json, task.header,
            trust_scores=[w.trust_score for w in workers],
        )

        # Décision produit : toujours payer hors gold standard, qu'il y ait
        # litige ou non -- seul le trust_score encaisse l'écart au consensus.
        task.eu_litige = consensus["status"] not in ("valide", "auto_valide")
        task.status = TaskStatus.completed
        task.resultat_final = chiffrer_json(
            construire_resultat_final(task.header, reponses_dechiffrees[0], consensus)
        )

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

TAILLE_MAX_FICHIER_OCTETS = 10 * 1024 * 1024  # 10 Mo -- large pour un CSV/Excel de nettoyage de données


def _sauver_upload_temporaire(fichier: UploadFile) -> str:
    """Sauvegarde le fichier uploadé (en flux, sans jamais le charger entier
    en mémoire), et le convertit en CSV s'il s'agit d'un Excel -- tout le
    reste du pipeline ne connaît que le CSV. Coupe court dès que la taille
    dépasse la limite, plutôt que de laisser un fichier énorme saturer le
    serveur ou le disque."""
    suffixe = os.path.splitext(fichier.filename or "")[1].lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffixe or ".csv")
    taille = 0
    try:
        with tmp as out:
            while True:
                morceau = fichier.file.read(1024 * 1024)  # 1 Mo par 1 Mo
                if not morceau:
                    break
                taille += len(morceau)
                if taille > TAILLE_MAX_FICHIER_OCTETS:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Fichier trop volumineux (max {TAILLE_MAX_FICHIER_OCTETS // (1024 * 1024)} Mo).",
                    )
                out.write(morceau)
    except HTTPException:
        os.remove(tmp.name)
        raise
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
            # Chiffrement au repos : raw_data, gold_answer et resultat_final
            # contiennent le contenu métier du client -- jamais stockés en
            # clair en base.
            t["raw_data"] = chiffrer_json(t["raw_data"])
            if t.get("gold_answer") is not None:
                t["gold_answer"] = chiffrer_json(t["gold_answer"])
            if t.get("resultat_final") is not None:
                t["resultat_final"] = chiffrer_json(t["resultat_final"])
            db.add(MicroTask(**t))
        db.commit()

        # La notification ne compte que le vrai travail restant pour les
        # workers -- les lignes déjà validées automatiquement ne doivent pas
        # gonfler artificiellement le volume annoncé.
        nb_pour_workers = sum(1 for t in tasks if t["status"] == "available")
        notifier_nouveau_projet(projet.titre, nb_pour_workers, projet.prix_par_ligne)

        nb_auto_validees = sum(1 for t in tasks if t["status"] == "completed")
        return {
            "nb_taches_creees": len(tasks),
            "nb_validees_automatiquement": nb_auto_validees,
            "nb_pour_workers": len(tasks) - nb_auto_validees,
        }
    finally:
        db.close()


DELAI_PURGE_JOURS = 7  # rétention des données brutes après le dernier export du client


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
        _journaliser_acces(db, user, "export_projet", project_id=project_id)

        from fastapi.responses import Response

        if format == "xlsx":
            import io as io_module

            import openpyxl

            classeur = openpyxl.Workbook()
            feuille = classeur.active
            feuille.append(taches[0].header)
            for t in taches:
                feuille.append(dechiffrer_json(t.resultat_final) or dechiffrer_json(t.raw_data))
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
            writer.writerow(dechiffrer_json(t.resultat_final) or dechiffrer_json(t.raw_data))
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="projet_{project_id}_nettoye.csv"'},
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ESPACE ADMIN -- validation des demandes de compte (client/worker)
# ---------------------------------------------------------------------------
# Remplace le script admin_tools.py au quotidien : un compte admin (créé une
# fois via `python3 admin_tools.py --creer-admin ...`) peut désormais
# approuver ou refuser une demande depuis /admin, sans terminal.

@app.get("/admin/comptes")
def admin_lister_comptes(
    statut: str = "en_attente",
    recherche: str | None = None,
    user: User = Depends(require_role("admin")),
):
    """statut: 'en_attente' (défaut) ou 'tous'. recherche : filtre par email (contient)."""
    db = SessionLocal()
    try:
        q = db.query(User).filter(User.role != RoleEnum.admin)
        if statut == "en_attente":
            q = q.filter_by(approuve=False)
        if recherche:
            q = q.filter(User.email.ilike(f"%{recherche.strip()}%"))
        comptes = q.order_by(User.id.desc()).all()
        return {
            "comptes": [
                {
                    "id": c.id,
                    "email": c.email,
                    "role": c.role.value,
                    "approuve": c.approuve,
                    "secteur_activite": c.secteur_activite,
                    "trust_score": round(c.trust_score, 1) if c.role == RoleEnum.worker else None,
                    "tasks_completed": c.tasks_completed if c.role == RoleEnum.worker else None,
                    "solde_disponible": round(c.solde_disponible, 2) if c.role == RoleEnum.worker else None,
                }
                for c in comptes
            ]
        }
    finally:
        db.close()


@app.get("/admin/paiements")
def admin_lister_paiements(user: User = Depends(require_role("admin"))):
    """Workers avec un solde à verser (> 0), triés par montant décroissant --
    le virement réel se fait hors plateforme (Wave/Orange Money/virement),
    ceci sert juste à savoir qui est éligible et à garder une trace."""
    db = SessionLocal()
    try:
        workers = (
            db.query(User)
            .filter(User.role == RoleEnum.worker, User.solde_disponible > 0)
            .order_by(User.solde_disponible.desc())
            .all()
        )
        return {
            "seuil_decaissement": SEUIL_DECAISSEMENT,
            "workers": [
                {
                    "id": w.id,
                    "email": w.email,
                    "solde_disponible": round(w.solde_disponible, 2),
                    "tasks_completed": w.tasks_completed,
                    "eligible": w.solde_disponible >= SEUIL_DECAISSEMENT,
                }
                for w in workers
            ],
        }
    finally:
        db.close()


@app.post("/admin/workers/{worker_id}/marquer-paye")
def admin_marquer_paye(worker_id: int, user: User = Depends(require_role("admin"))):
    """Ne déclenche AUCUN virement réel -- l'admin fait le virement lui-même
    (Wave, Orange Money, etc.) puis clique ici pour remettre le solde à zéro
    et garder une trace dans payouts."""
    db = SessionLocal()
    try:
        worker = db.query(User).filter_by(id=worker_id, role=RoleEnum.worker).first()
        if not worker:
            raise HTTPException(status_code=404, detail="Worker introuvable")
        if worker.solde_disponible <= 0:
            raise HTTPException(status_code=400, detail="Rien à verser pour ce compte")
        montant = worker.solde_disponible
        db.add(Payout(worker_id=worker.id, montant=montant))
        worker.solde_disponible = 0.0
        db.commit()
        return {"worker_id": worker.id, "montant_verse": round(montant, 2)}
    finally:
        db.close()


@app.get("/admin/projets")
def admin_lister_projets(user: User = Depends(require_role("admin"))):
    db = SessionLocal()
    try:
        projets = db.query(Project).order_by(Project.id.desc()).all()
        resultat = []
        for p in projets:
            taches = db.query(MicroTask).filter_by(project_id=p.id).all()
            client = db.query(User).get(p.client_id)
            resultat.append({
                "id": p.id,
                "titre": p.titre,
                "client_email": client.email if client else "?",
                "prix_par_ligne": p.prix_par_ligne,
                "total_lignes": len(taches),
                "lignes_completees": sum(1 for t in taches if t.status == TaskStatus.completed),
                "lignes_litigieuses": sum(1 for t in taches if t.eu_litige),
            })
        return {"projets": resultat}
    finally:
        db.close()


@app.get("/admin/projets/{project_id}/litiges")
def admin_lister_litiges(project_id: int, user: User = Depends(require_role("admin"))):
    """Détail des lignes en désaccord entre workers -- montre chaque réponse
    soumise (déchiffrée) pour permettre un arbitrage manuel éclairé."""
    db = SessionLocal()
    try:
        taches = db.query(MicroTask).filter_by(project_id=project_id, eu_litige=True).all()
        resultat = []
        for t in taches:
            submissions = db.query(Submission).filter_by(micro_task_id=t.id).all()
            resultat.append({
                "task_id": t.id,
                "row_id": t.row_id,
                "header": t.header,
                "resultat_final_actuel": dechiffrer_json(t.resultat_final),
                "soumissions": [
                    {
                        "worker_email": (db.query(User).get(s.worker_id) or User(email="?")).email,
                        "reponse": dechiffrer_json(s.reponse),
                    }
                    for s in submissions
                ],
            })
        return {"litiges": resultat}
    finally:
        db.close()


class ResoudreLitigeIn(BaseModel):
    valeurs: list[Annotated[str, StringConstraints(max_length=500)]] = Field(..., max_length=50)


@app.post("/admin/taches/{task_id}/resoudre-litige")
def admin_resoudre_litige(task_id: int, payload: ResoudreLitigeIn, user: User = Depends(require_role("admin"))):
    """L'admin tranche manuellement la valeur finale d'une ligne litigieuse --
    ne rejoue AUCUN paiement (déjà versé au moment du consensus initial),
    corrige seulement ce que le client recevra à l'export."""
    db = SessionLocal()
    try:
        tache = db.query(MicroTask).filter_by(id=task_id).first()
        if not tache:
            raise HTTPException(status_code=404, detail="Tâche introuvable")
        if len(payload.valeurs) != len(tache.header):
            raise HTTPException(status_code=400, detail=f"{len(tache.header)} valeur(s) attendue(s)")
        tache.resultat_final = chiffrer_json(payload.valeurs)
        tache.eu_litige = False
        db.commit()
        return {"task_id": tache.id, "resolu": True}
    finally:
        db.close()


@app.get("/admin/stats")
def admin_stats(user: User = Depends(require_role("admin"))):
    db = SessionLocal()
    try:
        return {
            "clients_approuves": db.query(User).filter_by(role=RoleEnum.client, approuve=True).count(),
            "workers_approuves": db.query(User).filter_by(role=RoleEnum.worker, approuve=True).count(),
            "comptes_en_attente": db.query(User).filter(User.role != RoleEnum.admin, User.approuve == False).count(),  # noqa: E712
            "projets_actifs": db.query(Project).count(),
        }
    finally:
        db.close()


@app.post("/admin/telegram/configurer-webhook")
def admin_configurer_webhook_telegram(user: User = Depends(require_role("admin"))):
    """À appeler UNE FOIS après déploiement (ou après changement de domaine)
    pour dire à Telegram où envoyer les messages reçus par le bot. Sans ça,
    la liaison de compte (/telegram/webhook) ne reçoit jamais rien."""
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN absent -- configure-le d'abord sur Render.")
    import httpx as _httpx
    url_cible = f"{os.environ.get('APP_BASE_URL', '').rstrip('/')}/telegram/webhook"
    if not url_cible.startswith("https://"):
        raise HTTPException(status_code=400, detail=f"APP_BASE_URL doit être une URL https publique (actuel : {url_cible!r}).")
    reponse = _httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
        json={"url": url_cible}, timeout=10,
    )
    return {"telegram_reponse": reponse.json(), "webhook_configure": url_cible}


@app.get("/admin/journal-acces")
def admin_journal_acces(project_id: int | None = None, user: User = Depends(require_role("admin"))):
    """Journal basique pour répondre à 'qui a accédé à quoi, quand' en cas
    de doute sur une fuite : connexions + exports (table access_logs), et
    qui a traité quelle ligne (table submissions, déjà existante -- on la
    joint ici pour l'exposer proprement plutôt que de dupliquer le suivi)."""
    db = SessionLocal()
    try:
        q_acces = db.query(AccessLog).order_by(AccessLog.created_at.desc())
        if project_id is not None:
            q_acces = q_acces.filter_by(project_id=project_id)
        acces = q_acces.limit(200).all()

        q_soumissions = (
            db.query(Submission, MicroTask, User)
            .join(MicroTask, Submission.micro_task_id == MicroTask.id)
            .join(User, Submission.worker_id == User.id)
            .order_by(Submission.created_at.desc())
        )
        if project_id is not None:
            q_soumissions = q_soumissions.filter(MicroTask.project_id == project_id)
        soumissions = q_soumissions.limit(200).all()

        titres_projets = {p.id: p.titre for p in db.query(Project).all()}

        return {
            "connexions_et_exports": [
                {
                    "email": a.email, "role": a.role, "action": a.action,
                    "projet": titres_projets.get(a.project_id) if a.project_id else None,
                    "date": a.created_at.isoformat(),
                }
                for a in acces
            ],
            "traitement_lignes": [
                {
                    "worker_email": worker.email,
                    "projet": titres_projets.get(tache.project_id),
                    "ligne": tache.row_id,
                    "date": sub.created_at.isoformat(),
                }
                for sub, tache, worker in soumissions
            ],
        }
    finally:
        db.close()


@app.post("/admin/comptes/{user_id}/approuver")
def admin_approuver_compte(user_id: int, user: User = Depends(require_role("admin"))):
    db = SessionLocal()
    try:
        cible = db.query(User).filter_by(id=user_id).first()
        if not cible:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        if cible.role == RoleEnum.admin:
            raise HTTPException(status_code=400, detail="Un compte admin ne se gère pas depuis cette interface")
        cible.approuve = True
        db.commit()
        return {"id": cible.id, "email": cible.email, "approuve": True}
    finally:
        db.close()


@app.post("/admin/comptes/{user_id}/refuser")
def admin_refuser_compte(user_id: int, user: User = Depends(require_role("admin"))):
    db = SessionLocal()
    try:
        cible = db.query(User).filter_by(id=user_id).first()
        if not cible:
            raise HTTPException(status_code=404, detail="Compte introuvable")
        if cible.role == RoleEnum.admin:
            raise HTTPException(status_code=400, detail="Un compte admin ne se gère pas depuis cette interface")
        cible.approuve = False
        db.commit()
        return {"id": cible.id, "email": cible.email, "approuve": False}
    finally:
        db.close()


@app.get("/admin", response_class=HTMLResponse)
def page_admin():
    with open("site/admin.html", encoding="utf-8") as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def page_depot_client():
    with open("site/client_upload.html", encoding="utf-8") as f:
        return f.read()


@app.get("/worker", response_class=HTMLResponse)
def page_worker_dashboard():
    with open("site/worker_dashboard.html", encoding="utf-8") as f:
        return f.read()
