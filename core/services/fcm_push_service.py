"""Best-effort FCM web push via Firebase Admin (optional; requires credentials file)."""

from __future__ import annotations

import logging
import os
from typing import Iterable

from django.conf import settings

logger = logging.getLogger(__name__)

_app_ready = False
_init_attempted = False


def _ensure_app() -> bool:
    global _app_ready, _init_attempted
    if _app_ready:
        return True
    if _init_attempted:
        return False
    _init_attempted = True
    path = getattr(settings, "FIREBASE_CREDENTIALS_PATH", "") or ""
    if not path or not os.path.isfile(path):
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        logger.debug("firebase_admin not installed; skipping FCM.")
        return False
    try:
        firebase_admin.get_app()
    except ValueError:
        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
    _app_ready = True
    return True


def send_fcm_to_tokens(tokens: Iterable[str], title: str, body: str) -> None:
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        t = (t or "").strip()
        if not t or len(t) > 8192 or t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    if not uniq:
        return
    if not _ensure_app():
        return
    from firebase_admin import messaging

    title = ((title or "").strip()[:200]) or "Notification"
    body = (body or "").strip()[:4000]
    chunk_size = 500
    for i in range(0, len(uniq), chunk_size):
        chunk = uniq[i : i + chunk_size]
        msg = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            tokens=chunk,
        )
        try:
            messaging.send_each_for_multicast(msg)
        except Exception:
            logger.exception("FCM multicast send failed")
