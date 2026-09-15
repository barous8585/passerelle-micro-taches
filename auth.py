"""
auth.py — Authentification basique par email/mot de passe (HTTP Basic Auth).

Choix volontairement simple pour un prototype :
  - PBKDF2-HMAC-SHA256 (natif Python, pas de dépendance native comme bcrypt
    à compiler) pour le hachage, avec sel aléatoire par utilisateur.
  - HTTP Basic Auth plutôt que JWT : pas de gestion d'expiration/refresh à
    coder pour la V1, le navigateur (ou le JS) renvoie email+mdp à chaque
    requête. À remplacer par des tokens de session si le prototype grandit.
"""

import hashlib
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from models import User, get_engine, get_session_factory

security = HTTPBasic()
_engine = get_engine()
_SessionLocal = get_session_factory(_engine)

PBKDF2_ITERATIONS = 100_000


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
        if not user or not verify_password(credentials.password, user.password_hash or ""):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Email ou mot de passe incorrect",
                headers={"WWW-Authenticate": "Basic"},
            )
        return user
    finally:
        db.close()


def require_role(role_attendu: str):
    """Dépendance FastAPI : n'autorise que les comptes du rôle donné
    ('client' ou 'worker'). Usage : Depends(require_role('client'))."""
    def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.value != role_attendu:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Cette action est réservée aux comptes '{role_attendu}'",
            )
        return user
    return _dependency
