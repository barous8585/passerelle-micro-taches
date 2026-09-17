"""
admin_tools.py — Outil en ligne de commande pour le tout premier compte
admin, seule porte d'entrée à l'espace /admin (voir README, section
"Espace admin").

Une fois CE compte créé, toute la validation des demandes (client/worker)
se fait depuis l'interface web /admin -- ce script ne sert plus qu'à
bootstrap le premier admin, ou en secours si /admin est inaccessible.

Usage :
    python3 admin_tools.py --creer-admin email@exemple.fr
    python3 admin_tools.py --lister
    python3 admin_tools.py --approuver email@exemple.fr
    python3 admin_tools.py --refuser email@exemple.fr
"""

import argparse
import getpass

from auth import hash_password
from models import RoleEnum, User, get_engine, get_session_factory


def creer_admin(db, email):
    if db.query(User).filter_by(email=email).first():
        print(f"Un compte existe déjà avec l'email {email}")
        return
    mot_de_passe = getpass.getpass("Mot de passe du compte admin : ")
    confirmation = getpass.getpass("Confirme le mot de passe : ")
    if mot_de_passe != confirmation:
        print("Les deux mots de passe ne correspondent pas -- rien n'a été créé.")
        return
    if len(mot_de_passe) < 8:
        print("Le mot de passe doit faire au moins 8 caractères -- rien n'a été créé.")
        return
    user = User(
        email=email,
        role=RoleEnum.admin,
        password_hash=hash_password(mot_de_passe),
        approuve=True,  # un admin s'auto-valide -- personne d'autre ne peut le faire pour le premier compte
    )
    db.add(user)
    db.commit()
    print(f"✅ Compte admin {email} créé. Connecte-toi sur /admin.")


def lister_comptes_en_attente(db):
    comptes = db.query(User).filter_by(approuve=False).order_by(User.id).all()
    if not comptes:
        print("Aucun compte en attente de validation.")
        return
    for u in comptes:
        extra = f" -- secteur : {u.secteur_activite}" if u.secteur_activite else ""
        print(f"[{u.id}] {u.email} ({u.role.value}){extra}")


def approuver(db, email):
    user = db.query(User).filter_by(email=email).first()
    if not user:
        print(f"Aucun compte avec l'email {email}")
        return
    user.approuve = True
    db.commit()
    print(f"✅ Compte {email} ({user.role.value}) approuvé.")


def refuser(db, email):
    """Ne supprime pas le compte -- le repasse juste à non-approuvé, au cas
    où une approbation aurait été faite par erreur."""
    user = db.query(User).filter_by(email=email).first()
    if not user:
        print(f"Aucun compte avec l'email {email}")
        return
    user.approuve = False
    db.commit()
    print(f"Compte {email} repassé en attente de validation.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gestion manuelle des comptes en attente de validation.")
    parser.add_argument("--creer-admin", metavar="EMAIL", help="Crée le premier compte admin (accès à /admin)")
    parser.add_argument("--lister", action="store_true", help="Liste les comptes en attente")
    parser.add_argument("--approuver", metavar="EMAIL", help="Approuve un compte par email")
    parser.add_argument("--refuser", metavar="EMAIL", help="Repasse un compte en attente")
    args = parser.parse_args()

    db = get_session_factory(get_engine())()
    try:
        if args.creer_admin:
            creer_admin(db, args.creer_admin)
        elif args.approuver:
            approuver(db, args.approuver)
        elif args.refuser:
            refuser(db, args.refuser)
        else:
            lister_comptes_en_attente(db)
    finally:
        db.close()
