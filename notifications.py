"""
notifications.py — Alerte le groupe Telegram des workers quand de nouvelles
micro-tâches deviennent disponibles.

Best-effort par conception : si le token n'est pas configuré, si Telegram
est injoignable, ou si le chat_id est invalide, on ne fait JAMAIS échouer
le dépôt du client pour ça -- la notification est un bonus, pas une
dépendance critique du flux métier.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()  # lit les variables depuis un fichier .env local (jamais commité)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")


def notifications_configurees() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


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

    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=5,
        )
    except httpx.HTTPError:
        pass  # échec réseau/config -- on ignore, le dépôt du client ne doit jamais en dépendre
