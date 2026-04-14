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
    token = (getattr(settings, "AAKASHSMS_AUTH_TOKEN", None) or "").strip()
    if token:
        from core.services.aakash_sms import AakashSmsError
        from core.services.aakash_sms import send_sms as aakash_send_sms

        try:
            aakash_send_sms(
                to=phone,
                text=f"Your KhudraPasal verification code is {code}. Do not share it with anyone.",
            )
        except AakashSmsError as e:
            logger.warning("OTP SMS rejected for %s: %s", phone, e)
            if settings.DEBUG:
                # Allow local/staging to complete flows when the gateway rejects or is misconfigured;
                # callers may expose the code only when DEBUG (see e.g. oauth_phone_send).
                logger.info("OTP SMS skipped after failure because DEBUG=True; code was still stored.")
                return
            err = str(e).lower()
            if "balance" in err or "not enough" in err:
                raise OTPError(
                    "SMS could not be sent due to a service limit. Please try again later."
                ) from e
            if "auth token" in err or ("token" in err and "valid" in err):
                raise OTPError("SMS service is not configured correctly. Please contact support.") from e
            raise OTPError(
                "Unable to send OTP to this number. Please check the number and try again."
            ) from e
    if settings.DEBUG:
        logger.info("OTP SMS (dev) to %s: %s", phone, code)


def send_template_sms(phone: str, message: str) -> None:
    """Transactional SMS (e.g. join-request rejection). Same dev hook as OTP."""
    text = (message or "").strip()
    if not text:
        return
    token = (getattr(settings, "AAKASHSMS_AUTH_TOKEN", None) or "").strip()
    if token:
        from core.services.aakash_sms import send_sms as aakash_send_sms

        aakash_send_sms(to=phone, text=text)
    if settings.DEBUG:
        logger.info("SMS (dev) to %s: %s", phone, text[:500])


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
