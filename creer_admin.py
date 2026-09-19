"""
creer_admin.py — Crée un compte administrateur directement dans la base de
production, sans passer par /auth/register (qui refuse volontairement le
rôle admin depuis le formulaire public d'inscription).

Usage : depuis la racine du dépôt, avec le venv activé :
    python3 creer_admin.py

Tout est saisi ici, localement -- rien n'est envoyé ni affiché ailleurs.
Le mot de passe est saisi en mode invisible (comme sudo).
"""

import getpass
import sys

sys.path.insert(0, ".")

from auth import hash_password
from models import RoleEnum, User, get_engine, get_session_factory

database_url = input(
    "URL de connexion à la base de PRODUCTION (External Database URL, "
    "copiée depuis Render -> ta base PostgreSQL -> Connections) : "
).strip()

email = input("Email du compte admin à créer : ").strip()
password = getpass.getpass("Mot de passe du compte admin (invisible en tapant) : ")
password2 = getpass.getpass("Confirme le mot de passe : ")

if not email or not password:
    print("Email et mot de passe sont obligatoires.")
    sys.exit(1)

if password != password2:
    print("Les deux mots de passe ne correspondent pas.")
    sys.exit(1)

engine = get_engine(database_url)
SessionLocal = get_session_factory(engine)
db = SessionLocal()

try:
    existant = db.query(User).filter_by(email=email).first()
    if existant:
        print(f"Un compte existe déjà avec cet email (rôle actuel : {existant.role.value}).")
        print("Aucune modification effectuée.")
        sys.exit(1)

    admin = User(
        email=email,
        role=RoleEnum.admin,
        password_hash=hash_password(password),
        approuve=True,  # un admin n'a pas besoin de validation manuelle
    )
    db.add(admin)
    db.commit()
    print(f"\n✅ Compte admin créé : {email}")
finally:
    db.close()
