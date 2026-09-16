"""
models.py — Schéma de base de données du prototype.

4 tables au cœur du système : users, projects, micro_tasks, submissions.
SQLite pour le prototype -> PostgreSQL en prod (aucun changement de code requis
grâce à SQLAlchemy, juste l'URL de connexion à changer).
"""

import datetime
import enum

from sqlalchemy import (JSON, Boolean, Column, DateTime, Enum, Float,
                         ForeignKey, Integer, String, UniqueConstraint,
                         create_engine, event)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class RoleEnum(str, enum.Enum):
    client = "client"
    worker = "worker"
    admin = "admin"


class TaskStatus(str, enum.Enum):
    available = "available"      # personne ne l'a prise
    assigned = "assigned"        # verrouillée, en cours de traitement
    completed = "completed"      # nombre de soumissions requis atteint et consensus validé
    disputed = "disputed"        # désaccord entre workers, besoin d'arbitrage


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=True)  # PBKDF2 "salt$hash", voir auth.py
    role = Column(Enum(RoleEnum), nullable=False)
    trust_score = Column(Float, default=80.0)  # démarre à 80/100, évolue avec l'historique
    tasks_completed = Column(Integer, default=0)
    solde_disponible = Column(Float, default=0.0)  # wallet interne -- pas de virement direct par ligne
    accepte_confidentialite = Column(Boolean, default=False)  # engagement à ne pas copier/exporter les données traitées
    date_acceptation_confidentialite = Column(DateTime, nullable=True)


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True)
    client_id = Column(Integer, ForeignKey("users.id"))
    titre = Column(String, nullable=False)
    schema_json = Column(JSON, nullable=False)
    prix_par_ligne = Column(Float, default=0.04)
    date_dernier_export = Column(DateTime, nullable=True)  # sert de point de départ au délai de purge des données brutes


class MicroTask(Base):
    __tablename__ = "micro_tasks"
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"))
    row_id = Column(Integer, nullable=False)
    raw_data = Column(JSON, nullable=False)
    header = Column(JSON, nullable=False)
    badges = Column(JSON, default=list)
    schema_json = Column(JSON, nullable=False)
    redundancy_level = Column(Integer, default=1)
    is_gold_standard = Column(Boolean, default=False)
    gold_answer = Column(JSON, nullable=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.available)
    eu_litige = Column(Boolean, default=False)  # pour reporting admin -- n'empêche PAS le paiement
    resultat_final = Column(JSON, nullable=True)  # valeur consensuelle retenue une fois complétée -- c'est CE que le client récupère à l'export

    # Verrouillage anti-doublon : qui a pris la tâche et depuis quand
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    assigned_at = Column(DateTime, nullable=True)

    submissions = relationship("Submission", back_populates="task")


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # Un worker ne peut soumettre qu'UNE SEULE fois par tâche -- contrainte
        # au niveau base de données (pas juste une vérification applicative)
        # pour fermer aussi la fenêtre de course entre deux requêtes simultanées.
        UniqueConstraint("micro_task_id", "worker_id", name="uq_une_soumission_par_worker_et_tache"),
    )
    id = Column(Integer, primary_key=True)
    micro_task_id = Column(Integer, ForeignKey("micro_tasks.id"))
    worker_id = Column(Integer, ForeignKey("users.id"))
    reponse = Column(JSON, nullable=False)  # la ligne corrigée
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    montant = Column(Float, nullable=True)  # None tant que non payé (redondance pas encore atteinte)

    task = relationship("MicroTask", back_populates="submissions")


def get_engine(db_url="sqlite:///./passerelle.db"):
    if "sqlite" not in db_url:
        return create_engine(db_url)

    # timeout=30 : une connexion qui trouve la base verrouillée RÉESSAIE pendant
    # 30s au lieu d'échouer immédiatement avec "database is locked".
    engine = create_engine(db_url, connect_args={"check_same_thread": False, "timeout": 30})

    # Mode WAL : permet à des lectures et une écriture de se dérouler en même
    # temps sans se bloquer mutuellement -- le vrai correctif pour un usage
    # concurrent (plusieurs onglets/utilisateurs) avec SQLite.
    @event.listens_for(engine, "connect")
    def _activer_wal(connexion_dbapi, _):
        curseur = connexion_dbapi.cursor()
        curseur.execute("PRAGMA journal_mode=WAL")
        curseur.execute("PRAGMA busy_timeout=30000")
        curseur.close()

    return engine


def get_session_factory(engine):
    Base.metadata.create_all(engine)
    # expire_on_commit=False : évite les DetachedInstanceError quand on lit
    # un attribut (ex: .id) après un commit sur une session déjà fermée.
    return sessionmaker(bind=engine, expire_on_commit=False)
