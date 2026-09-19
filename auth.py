"""
auth.py — Authentification par jeton de session temporaire.

Avant : HTTP Basic Auth, où le navigateur/JS renvoyait email+mot de passe à
CHAQUE requête. Pratique mais dangereux : si ce header fuite une seule fois
(log mal configuré, extension de navigateur compromise, etc.), c'est le mot
de passe lui-même qui est exposé, valable indéfiniment.

Maintenant : /auth/login vérifie email+mot de passe UNE FOIS et renvoie un
jeton aléatoire (SessionToken) valable DUREE_SESSION_HEURES. Toutes les
requêtes suivantes n'envoient plus que ce jeton (Authorization: Bearer ...).
Un jeton qui fuite expire tout seul, et peut être révoqué immédiatement à
la déconnexion -- contrairement à un mot de passe, il n'offre qu'un accès
limité dans le temps et coupable à volonté.

Hachage des mots de passe : PBKDF2-HMAC-SHA256 (natif Python, pas de
dépendance native comme bcrypt à compiler), sel aléatoire par utilisateur.
"""

import datetime
import hashlib
import os
import secrets
import string

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from models import SessionToken, User

# Le moteur/session ne sont PAS créés ici -- api.py les crée une seule fois et
# les injecte via init_auth_db(). Avoir deux moteurs séparés (un par module)
# pointant sur le même fichier SQLite doublait inutilement les connexions
# concurrentes et aggravait les erreurs "database is locked".
_SessionLocal = None


def init_auth_db(session_factory):
    global _SessionLocal
    _SessionLocal = session_factory


security = HTTPBearer()

PBKDF2_ITERATIONS = 100_000

SEUIL_TENTATIVES_ECHOUEES = 5   # au-delà, le compte est verrouillé temporairement
DUREE_VERROUILLAGE_MINUTES = 15

DUREE_SESSION_HEURES = 12   # au-delà, le jeton expire -- reconnexion requise

DUREE_CODE_LIAISON_MINUTES = 10
DUREE_OTP_MINUTES = 10
SEUIL_OTP_TENTATIVES = 5


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


def authentifier(db, email: str, password: str) -> User:
    """Vérifie email + mot de passe et applique le verrouillage anti-brute-
    force. Utilisé UNE SEULE FOIS, à la connexion (/auth/login) -- pas à
    chaque requête, contrairement à l'ancien système.

    Une fois Telegram lié (telegram_chat_id renseigné), le mot de passe
    n'est PLUS accepté pour se connecter -- seul le code envoyé via
    Telegram fonctionne (voir generer_otp/verifier_otp). Ça évite qu'un mot
    de passe compromis reste une porte d'entrée valable après la liaison."""
    user = db.query(User).filter_by(email=email).first()

    if user and user.telegram_chat_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ce compte se connecte désormais par code Telegram, plus par mot de passe.",
        )

    if user and user.verrouille_jusqua and user.verrouille_jusqua > datetime.datetime.utcnow():
        minutes_restantes = int((user.verrouille_jusqua - datetime.datetime.utcnow()).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Trop de tentatives échouées. Réessaie dans {minutes_restantes} min.",
        )

    if not user or not verify_password(password, user.password_hash or ""):
        if user:
            user.tentatives_echouees = (user.tentatives_echouees or 0) + 1
            if user.tentatives_echouees >= SEUIL_TENTATIVES_ECHOUEES:
                user.verrouille_jusqua = datetime.datetime.utcnow() + datetime.timedelta(minutes=DUREE_VERROUILLAGE_MINUTES)
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou mot de passe incorrect",
        )

    if user.tentatives_echouees:
        user.tentatives_echouees = 0
        user.verrouille_jusqua = None
        db.commit()

    return user


def creer_session(db, user: User) -> str:
    """Génère un nouveau jeton de session aléatoire (32 octets, imprévisible)
    et le stocke avec sa date d'expiration. Renvoie le jeton en clair --
    c'est la SEULE fois qu'il existe en clair, à partir de là seule sa
    présence en base compte."""
    jeton = secrets.token_hex(32)
    db.add(SessionToken(
        token=jeton, user_id=user.id,
        expires_at=datetime.datetime.utcnow() + datetime.timedelta(hours=DUREE_SESSION_HEURES),
    ))
    db.commit()
    return jeton


def revoquer_session(db, jeton: str) -> None:
    """Invalide un jeton immédiatement (déconnexion explicite) -- inutile
    d'attendre son expiration naturelle."""
    db.query(SessionToken).filter_by(token=jeton).delete()
    db.commit()


# ---------------------------------------------------------------------------
# LIAISON TELEGRAM -- un utilisateur déjà connecté (par mot de passe, avant
# la liaison) génère un code à coller dans le bot Telegram. Le webhook
# Telegram (voir api.py /telegram/webhook) reçoit ce code et finalise la
# liaison en enregistrant le chat_id privé de l'utilisateur.
# ---------------------------------------------------------------------------

def generer_code_liaison(db, user: User) -> str:
    code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    user.telegram_code_liaison = code
    user.telegram_liaison_expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=DUREE_CODE_LIAISON_MINUTES)
    db.commit()
    return code


def lier_via_code(db, code: str, chat_id: str) -> User | None:
    """Appelé par le webhook Telegram quand quelqu'un envoie un message au
    bot. Si le texte reçu correspond à un code de liaison valide (pas
    expiré), on enregistre son chat_id -- renvoie None si le code est
    invalide/expiré/inconnu, pour que le webhook ignore silencieusement les
    messages qui ne sont pas des codes de liaison (spam, "/start" seul, etc.)."""
    code = (code or "").strip().upper()
    if not code:
        return None
    user = db.query(User).filter_by(telegram_code_liaison=code).first()
    if not user or not user.telegram_liaison_expire or user.telegram_liaison_expire < datetime.datetime.utcnow():
        return None
    user.telegram_chat_id = str(chat_id)
    user.telegram_code_liaison = None
    user.telegram_liaison_expire = None
    db.commit()
    return user


# ---------------------------------------------------------------------------
# CONNEXION PAR CODE TELEGRAM (OTP) -- remplace le mot de passe une fois
# Telegram lié. Un code à 6 chiffres, valable DUREE_OTP_MINUTES, envoyé en
# message privé -- voir notifications.envoyer_code_connexion.
# ---------------------------------------------------------------------------

def generer_otp(db, user: User) -> str:
    code = "".join(secrets.choice(string.digits) for _ in range(6))
    user.otp_code = code
    user.otp_expire = datetime.datetime.utcnow() + datetime.timedelta(minutes=DUREE_OTP_MINUTES)
    user.otp_tentatives = 0
    db.commit()
    return code


def verifier_otp(db, email: str, code: str) -> User:
    user = db.query(User).filter_by(email=email).first()

    if user and user.verrouille_jusqua and user.verrouille_jusqua > datetime.datetime.utcnow():
        minutes_restantes = int((user.verrouille_jusqua - datetime.datetime.utcnow()).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Trop de tentatives échouées. Réessaie dans {minutes_restantes} min.",
        )

    code_valide = (
        user and user.otp_code and user.otp_expire
        and user.otp_expire > datetime.datetime.utcnow()
        and user.otp_code == (code or "").strip()
    )
    if not code_valide:
        if user:
            user.otp_tentatives = (user.otp_tentatives or 0) + 1
            if user.otp_tentatives >= SEUIL_OTP_TENTATIVES:
                user.verrouille_jusqua = datetime.datetime.utcnow() + datetime.timedelta(minutes=DUREE_VERROUILLAGE_MINUTES)
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Code invalide ou expiré")

    user.otp_code = None
    user.otp_expire = None
    user.otp_tentatives = 0
    db.commit()
    return user


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    db = _SessionLocal()
    try:
        session = db.query(SessionToken).filter_by(token=credentials.credentials).first()

        if not session or session.expires_at < datetime.datetime.utcnow():
            if session:  # jeton expiré -- on nettoie plutôt que de le laisser traîner
                db.delete(session)
                db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expirée ou invalide, reconnecte-toi.",
            )

        user = db.query(User).filter_by(id=session.user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Compte introuvable")

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
