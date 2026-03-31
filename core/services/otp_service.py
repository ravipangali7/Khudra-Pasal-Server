from __future__ import annotations

import logging
import random
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.models import OTPVerification

logger = logging.getLogger(__name__)


class OTPError(Exception):
    pass


def _generate_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def send_otp_sms(phone: str, code: str) -> None:
    if settings.DEBUG:
        logger.info("OTP SMS (dev) to %s: %s", phone, code)
    # Production: plug SMS provider via settings / env.


def send_template_sms(phone: str, message: str) -> None:
    """Transactional SMS (e.g. join-request rejection). Same dev hook as OTP."""
    text = (message or "").strip()
    if not text:
        return
    if settings.DEBUG:
        logger.info("SMS (dev) to %s: %s", phone, text[:500])
    # Production: plug SMS provider via settings / env.


@transaction.atomic
def create_otp(phone: str, purpose: str, signup_name: str = "") -> OTPVerification:
    code = _generate_code()
    expires_at = timezone.now() + timedelta(minutes=10)
    row = OTPVerification.objects.create(
        phone=phone,
        otp=code,
        purpose=purpose,
        signup_name=(signup_name or "")[:150],
        expires_at=expires_at,
    )
    send_otp_sms(phone, code)
    return row


@transaction.atomic
def consume(phone: str, purpose: str, code: str) -> OTPVerification:
    row = (
        OTPVerification.objects.select_for_update()
        .filter(phone=phone, purpose=purpose, otp=code, is_used=False)
        .order_by("-created_at")
        .first()
    )
    if not row:
        raise OTPError("Invalid or unknown OTP")
    if row.expires_at < timezone.now():
        raise OTPError("OTP has expired")
    row.is_used = True
    row.save(update_fields=["is_used"])
    return row
