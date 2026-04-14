"""Complete OAuth sign-in after the user verifies a Nepal mobile number (OTP)."""

from __future__ import annotations

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.models import OTPVerification, User
from core.phone_auth import find_user_by_phone_input, normalize_nepal_phone
from core.portal_roles import infer_portal_key_from_frontend_path
from core.services import otp_service
from core.throttles import OtpSendThrottle
from core.views.social_oauth import _effective_portal_key_for_oauth, read_oauth_pending_token
from core.views.unified_auth import build_auth_response_for_portal


@api_view(["POST"])
@throttle_classes([OtpSendThrottle])
@permission_classes([AllowAny])
def oauth_phone_send(request):
    raw_token = (request.data.get("pending_token") or "").strip()
    raw_phone = (request.data.get("phone") or "").strip()
    phone = normalize_nepal_phone(raw_phone)
    if not raw_token or not phone:
        return Response(
            {"detail": "pending_token and a valid Nepal mobile number are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        uid, _next_path = read_oauth_pending_token(raw_token)
    except signing.BadSignature:
        return Response(
            {"detail": "Invalid or expired session. Try signing in with Google again."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.filter(pk=uid).first()
    if not user or not user.is_active:
        return Response({"detail": "Account not available."}, status=status.HTTP_400_BAD_REQUEST)
    if user.oauth_phone_completed:
        return Response(
            {"detail": "Phone verification is already complete. Try signing in again."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    other = find_user_by_phone_input(phone)
    if other and other.pk != user.pk:
        return Response(
            {"detail": "This number is already registered to another account."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        otp_service.create_otp(phone, OTPVerification.Purpose.OAUTH_PHONE, "")
    except otp_service.OTPError as e:
        return Response({"detail": str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    payload = {"detail": "OTP sent."}
    if settings.DEBUG:
        latest = (
            OTPVerification.objects.filter(
                phone=phone,
                purpose=OTPVerification.Purpose.OAUTH_PHONE,
                is_used=False,
            )
            .order_by("-created_at")
            .first()
        )
        if latest:
            payload["debug_otp"] = latest.otp
    return Response(payload)


@api_view(["POST"])
@permission_classes([AllowAny])
def oauth_phone_verify(request):
    raw_token = (request.data.get("pending_token") or "").strip()
    raw_phone = (request.data.get("phone") or "").strip()
    code = (request.data.get("otp") or "").strip()
    phone = normalize_nepal_phone(raw_phone)
    if not raw_token or not phone or len(code) != 6 or not code.isdigit():
        return Response(
            {"detail": "pending_token, phone, and a 6-digit OTP are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        uid, next_path = read_oauth_pending_token(raw_token)
    except signing.BadSignature:
        return Response(
            {"detail": "Invalid or expired session. Try signing in with Google again."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        otp_service.consume(phone, OTPVerification.Purpose.OAUTH_PHONE, code)
    except otp_service.OTPError as e:
        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=uid).first()
        if not user or not user.is_active:
            return Response({"detail": "Account not available."}, status=status.HTTP_400_BAD_REQUEST)
        if user.oauth_phone_completed:
            return Response(
                {"detail": "Phone verification is already complete. Try signing in again."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        other = find_user_by_phone_input(phone)
        if other and other.pk != user.pk:
            return Response(
                {"detail": "This number is already registered to another account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.phone = phone
        user.oauth_phone_completed = True
        user.last_login = timezone.now()
        user.save(update_fields=["phone", "oauth_phone_completed", "last_login"])

    requested_portal = infer_portal_key_from_frontend_path(next_path)
    portal_key = _effective_portal_key_for_oauth(user, next_path)
    data = build_auth_response_for_portal(user, portal_key)
    if isinstance(data, Response):
        detail = getattr(data, "data", None) or {}
        msg = detail.get("detail", "Sign-in not allowed for this portal.")
        return Response({"detail": str(msg)}, status=data.status_code)
    redirect_final = data["redirect"]
    if portal_key == requested_portal and next_path.startswith("/") and not next_path.startswith("//"):
        legacy_dashboard = next_path.strip() == "/portal/dashboard"
        canonical = str(redirect_final or "").rstrip("/") == "/portal"
        if not (legacy_dashboard and canonical):
            redirect_final = next_path
    out = dict(data)
    out["redirect"] = redirect_final
    return Response(out)

