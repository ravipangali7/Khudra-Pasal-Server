"""FCM push via Firebase Admin (requires service account JSON on the server)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable

from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_app_ready = False
_init_attempted = False
_warned_missing_credentials = False


def _resolve_credentials_path() -> str:
    """Return an existing service-account JSON path, or '' if none found."""
    raw = (getattr(settings, "FIREBASE_CREDENTIALS_PATH", "") or "").strip()
    candidates: list[Path] = []
    if raw:
        p = Path(raw)
        candidates.append(p if p.is_absolute() else Path.cwd() / p)
    base = Path(getattr(settings, "BASE_DIR", Path.cwd()))
    candidates.extend(
        [
            base.parent / "firebase-service.json",
            base / "firebase-service.json",
        ]
    )
    seen: set[str] = set()
    for c in candidates:
        key = str(c.resolve())
        if key in seen:
            continue
        seen.add(key)
        if c.is_file():
            return key
    return raw


def _ensure_app() -> bool:
    global _app_ready, _init_attempted, _warned_missing_credentials
    if _app_ready:
        return True
    if _init_attempted:
        return False
    _init_attempted = True
    path = _resolve_credentials_path()
    if not path or not os.path.isfile(path):
        if not _warned_missing_credentials:
            _warned_missing_credentials = True
            logger.warning(
                "FCM disabled: FIREBASE_CREDENTIALS_PATH missing or not a file (%r). "
                "Set env FIREBASE_CREDENTIALS_PATH to your Firebase service account JSON.",
                path or "(empty)",
            )
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        logger.error(
            "FCM disabled: firebase_admin not installed. Add firebase-admin to requirements.txt."
        )
        return False
    try:
        firebase_admin.get_app()
    except ValueError:
        cred = credentials.Certificate(path)
        firebase_admin.initialize_app(cred)
    _app_ready = True
    logger.info("Firebase Admin initialized for FCM (credentials: %s)", path)
    return True


@dataclass
class FcmPushStats:
    """Result of attempting to push to device tokens (for API responses and ops logs)."""

    firebase_configured: bool
    unique_tokens: int
    success_count: int
    failure_count: int
    skip_reason: str | None
    first_error: str | None


def send_fcm_to_tokens(tokens: Iterable[str], title: str, body: str) -> FcmPushStats:
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        t = (t or "").strip()
        if not t or len(t) > 8192 or t in seen:
            continue
        seen.add(t)
        uniq.append(t)

    fb_ok = _ensure_app()

    if not uniq:
        return FcmPushStats(
            firebase_configured=fb_ok,
            unique_tokens=0,
            success_count=0,
            failure_count=0,
            skip_reason="no_device_tokens",
            first_error=None,
        )

    if not fb_ok:
        return FcmPushStats(
            firebase_configured=False,
            unique_tokens=len(uniq),
            success_count=0,
            failure_count=0,
            skip_reason="firebase_not_configured",
            first_error=None,
        )

    from firebase_admin import messaging

    title = ((title or "").strip()[:200]) or "Notification"
    body = (body or "").strip()[:4000]

    # High priority helps delivery when the app is backgrounded / Doze (Android).
    android = messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            default_sound=True,
            default_vibrate_timings=True,
        ),
    )
    webpush = messaging.WebpushConfig(
        notification=messaging.WebpushNotification(title=title, body=body),
    )

    total_success = 0
    total_failure = 0
    first_err: str | None = None
    chunk_size = 500

    data = {"title": title, "body": body}

    for i in range(0, len(uniq), chunk_size):
        chunk = uniq[i : i + chunk_size]
        msg = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            data=data,
            android=android,
            webpush=webpush,
            tokens=chunk,
        )
        try:
            br = messaging.send_each_for_multicast(msg)
        except Exception as e:
            logger.exception("FCM multicast send raised: %s", e)
            total_failure += len(chunk)
            if first_err is None:
                first_err = str(e)[:500]
            continue

        total_success += br.success_count
        total_failure += br.failure_count

        stale_tokens: list[str] = []
        for idx, resp in enumerate(br.responses):
            if resp.success:
                continue
            ex = resp.exception
            if ex is not None and first_err is None:
                first_err = str(ex)[:500]
            if ex is not None:
                logger.warning("FCM token send failed: %s", ex)
                code = getattr(ex, "code", "") or ""
                if code in ("UNREGISTERED", "INVALID_ARGUMENT", "NOT_FOUND"):
                    stale_tokens.append(chunk[idx])
        if stale_tokens:
            from core.models import User

            User.objects.filter(fcm_token__in=stale_tokens).update(fcm_token="")

    if total_failure:
        logger.warning(
            "FCM broadcast partial failure: success=%s failure=%s first_error=%s",
            total_success,
            total_failure,
            first_err,
        )
    else:
        logger.info("FCM broadcast ok: success=%s tokens=%s", total_success, len(uniq))

    return FcmPushStats(
        firebase_configured=True,
        unique_tokens=len(uniq),
        success_count=total_success,
        failure_count=total_failure,
        skip_reason=None if total_failure == 0 else "some_tokens_rejected",
        first_error=first_err,
    )
