"""Tests du module de notifications Telegram -- vérifie le comportement
best-effort (jamais d'exception qui remonte) sans dépendre d'un vrai bot."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_notification_sans_configuration_ne_leve_pas_d_exception(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    import importlib

    import notifications
    importlib.reload(notifications)

    assert notifications.notifications_configurees() is False
    notifications.notifier_nouveau_projet("Test", 5, 0.05)  # ne doit rien lever


def test_notification_avec_token_invalide_ne_leve_pas_d_exception(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faux_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1")
    import importlib

    import notifications
    importlib.reload(notifications)

    assert notifications.notifications_configurees() is True
    notifications.notifier_nouveau_projet("Test", 5, 0.05)  # échoue côté Telegram, mais silencieusement
