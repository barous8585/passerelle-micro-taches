"""
admin_tools.py — Outil en ligne de commande pour valider manuellement les
comptes client et worker avant qu'ils puissent utiliser la plateforme.

Pas d'interface web pour l'instant (voir README, section "Statut") -- un
script suffit pour un lancement pilote où tu approuves toi-même chaque
compte. Un vrai tableau de bord admin reste un chantier à part.

Usage :
    python3 admin_tools.py --lister
    python3 admin_tools.py --approuver email@exemple.fr
    python3 admin_tools.py --refuser email@exemple.fr
"""

import argparse

from models import User, get_engine, get_session_factory


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
    parser.add_argument("--lister", action="store_true", help="Liste les comptes en attente")
    parser.add_argument("--approuver", metavar="EMAIL", help="Approuve un compte par email")
    parser.add_argument("--refuser", metavar="EMAIL", help="Repasse un compte en attente")
    args = parser.parse_args()

    db = get_session_factory(get_engine())()
    try:
        if args.approuver:
            approuver(db, args.approuver)
        elif args.refuser:
            refuser(db, args.refuser)
        else:
            lister_comptes_en_attente(db)
    finally:
        db.close()
