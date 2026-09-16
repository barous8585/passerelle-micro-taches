"""
auth.py — Authentification basique par email/mot de passe (HTTP Basic Auth).

Choix volontairement simple pour un prototype :
  - PBKDF2-HMAC-SHA256 (natif Python, pas de dépendance native comme bcrypt
    à compiler) pour le hachage, avec sel aléatoire par utilisateur.
  - HTTP Basic Auth plutôt que JWT : pas de gestion d'expiration/refresh à
    coder pour la V1, le navigateur (ou le JS) renvoie email+mdp à chaque
    requête. À remplacer par des tokens de session si le prototype grandit.
"""

import datetime
import hashlib
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from models import User

# Le moteur/session ne sont PAS créés ici -- api.py les crée une seule fois et
# les injecte via init_auth_db(). Avoir deux moteurs séparés (un par module)
# pointant sur le même fichier SQLite doublait inutilement les connexions
# concurrentes et aggravait les erreurs "database is locked".
_SessionLocal = None


def init_auth_db(session_factory):
    global _SessionLocal
    _SessionLocal = session_factory


security = HTTPBasic()

PBKDF2_ITERATIONS = 100_000

SEUIL_TENTATIVES_ECHOUEES = 5   # au-delà, le compte est verrouillé temporairement
DUREE_VERROUILLAGE_MINUTES = 15


def hash_password(password: str) -> str:
    sel = os.urandom(16)
    empreinte = hashlib.pbkdf2_hmac("sha256", password.encode(), sel, PBKDF2_ITERATIONS)
    return f"{sel.hex()}${empreinte.hex()}"


def verify_password(password: str, stocke: str) -> bool:
    try:
        sel_hex, empreinte_hex = stocke.split("$")
    except (ValueError, AttributeError):
        return False
    sel = bytes.fromhex(sel_hex)
    empreinte = hashlib.pbkdf2_hmac("sha256", password.encode(), sel, PBKDF2_ITERATIONS)
    return empreinte.hex() == empreinte_hex


def get_current_user(credentials: HTTPBasicCredentials = Depends(security)) -> User:
    db = _SessionLocal()
    try:
        user = db.query(User).filter_by(email=credentials.username).first()

        # Compte temporairement verrouillé -- refusé même si le bon mot de
        # passe est fourni, tant que le délai n'est pas écoulé. Empêche un
        # script de tester des milliers de mots de passe sur un même compte.
        if user and user.verrouille_jusqua and user.verrouille_jusqua > datetime.datetime.utcnow():
            minutes_restantes = int((user.verrouille_jusqua - datetime.datetime.utcnow()).total_seconds() / 60) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Trop de tentatives échouées. Réessaie dans {minutes_restantes} min.",
            )

        if not user or not verify_password(credentials.password, user.password_hash or ""):
            if user:
                user.tentatives_echouees = (user.tentatives_echouees or 0) + 1
                if user.tentatives_echouees >= SEUIL_TENTATIVES_ECHOUEES:
                    user.verrouille_jusqua = datetime.datetime.utcnow() + datetime.timedelta(minutes=DUREE_VERROUILLAGE_MINUTES)
                db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Email ou mot de passe incorrect",
                headers={"WWW-Authenticate": "Basic"},
            )

        # Connexion réussie -- on réinitialise le compteur
        if user.tentatives_echouees:
            user.tentatives_echouees = 0
            user.verrouille_jusqua = None
            db.commit()

        return user
    finally:
        db.close()


def require_role(role_attendu: str):
    """Dépendance FastAPI : n'autorise que les comptes du rôle donné
    ('client' ou 'worker'), ET déjà validés manuellement par l'équipe.
    Usage : Depends(require_role('client'))."""
    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.value != role_attendu:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Cette action est réservée aux comptes '{role_attendu}'",
            )
        if not user.approuve:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Ton compte est en attente de validation par l'équipe Passerelle. Tu seras prévenu une fois activé.",
            )
        return user
    return _dependency
