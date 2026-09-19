"""
notifications.py — Intégration Telegram : alerte le groupe des workers
quand de nouvelles micro-tâches deviennent disponibles, ET envoie des
messages privés individuels (liaison de compte, codes de connexion --
voir auth.py / api.py).

Best-effort par conception : si le token n'est pas configuré, si Telegram
est injoignable, ou si le chat_id est invalide, on ne fait JAMAIS échouer
l'action réelle (dépôt client, connexion) pour ça.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()  # lit les variables depuis un fichier .env local (jamais commité)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # groupe de diffusion (nouvelles tâches)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

# Visible une seule fois au démarrage du serveur -- le signal le plus utile
# pour diagnostiquer un .env introuvable ou mal rempli, avant même de tenter
# un dépôt de fichier.
if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    print(f"✅ Notifications Telegram activées (chat_id={TELEGRAM_CHAT_ID})")
else:
    print("ℹ️  Notifications Telegram désactivées (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID absents du .env)")


def notifications_configurees() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def bot_configure() -> bool:
    """Différent de notifications_configurees() : ne vérifie QUE le token du
    bot, pas le chat_id du groupe de diffusion -- utilisé pour les messages
    privés (liaison de compte, codes de connexion), qui n'ont pas besoin du
    groupe pour fonctionner."""
    return bool(TELEGRAM_BOT_TOKEN)


def envoyer_message_telegram(chat_id: str, texte: str) -> bool:
    """Envoie un message privé à un chat_id individuel (pas le groupe de
    diffusion). Renvoie True/False plutôt que de lever une exception --
    l'appelant décide comment réagir à un échec (ex: le worker doit être
    prévenu si son code de connexion n'a pas pu partir)."""
    if not bot_configure():
        return False
    try:
        reponse = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": texte},
            timeout=5,
        )
        if reponse.status_code != 200:
            print(f"⚠️  Telegram a refusé l'envoi ({reponse.status_code}) : {reponse.text}")
            return False
        return True
    except httpx.HTTPError as e:
        print(f"⚠️  Impossible de joindre Telegram : {e}")
        return False


def notifier_nouveau_projet(titre: str, nb_taches: int, prix_par_ligne: float):
    """Appelée après l'ingestion réussie d'un CSV/Excel -- prévient le
    groupe qu'il y a du travail disponible, avec le volume et le tarif."""
    if not notifications_configurees():
        return  # notifications désactivées (pas de token/chat_id) -- silencieux, pas une erreur

    gain_estime = nb_taches * prix_par_ligne
    message = (
        "🔔 Nouvelles micro-tâches disponibles !\n\n"
        f"📁 {titre}\n"
        f"📋 {nb_taches} ligne(s) à traiter\n"
        f"💰 {prix_par_ligne:.2f} €/ligne (~{gain_estime:.2f} € au total sur ce lot)\n\n"
        f"👉 {APP_BASE_URL}/worker"
    )
    envoyer_message_telegram(TELEGRAM_CHAT_ID, message)


def envoyer_code_connexion(chat_id: str, code: str) -> bool:
    """Message privé envoyé à chaque tentative de connexion d'un compte lié
    à Telegram -- ce code remplace le mot de passe, valable 10 minutes."""
    message = (
        "🔐 Code de connexion Passerelle\n\n"
        f"Ton code : {code}\n\n"
        "Valable 10 minutes. Ne le partage avec personne -- l'équipe "
        "Passerelle ne te le demandera jamais par un autre canal."
    )
    return envoyer_message_telegram(chat_id, message)


def envoyer_confirmation_liaison(chat_id: str) -> bool:
    return envoyer_message_telegram(
        chat_id,
        "✅ Ton compte Telegram est maintenant lié à ton compte Passerelle. "
        "Tes prochaines connexions se feront par code envoyé ici, plus par mot de passe.",
    )


def notifier_nouvelle_inscription(email: str, role: str, secteur_activite: str | None = None) -> None:
    """Alerte le groupe de diffusion à chaque nouvelle inscription (client ou
    worker), pour que l'équipe soit prévenue en temps réel au lieu de devoir
    aller vérifier le panneau admin manuellement -- sans ça, un compte peut
    attendre des jours avant validation."""
    if not notifications_configurees():
        return

    if role == "client":
        message = (
            "🏢 Nouvelle inscription entreprise\n\n"
            f"📧 {email}\n"
            f"🏷️ Secteur : {secteur_activite or 'non renseigné'}\n\n"
            f"👉 {APP_BASE_URL}/admin"
        )
    else:
        message = (
            "🎓 Nouvelle inscription étudiant(e)\n\n"
            f"📧 {email}\n\n"
            f"👉 {APP_BASE_URL}/admin"
        )
    envoyer_message_telegram(TELEGRAM_CHAT_ID, message)


def envoyer_confirmation_approbation(chat_id: str) -> bool:
    """Prévient un compte déjà lié à Telegram dès que son inscription est
    validée -- évite le silence radio pendant l'attente de validation
    manuelle pour ceux qui ont déjà lié leur compte."""
    return envoyer_message_telegram(
        chat_id,
        "🎉 Ton compte Passerelle vient d'être validé par l'équipe -- tu peux "
        "maintenant l'utiliser normalement.",
    )
