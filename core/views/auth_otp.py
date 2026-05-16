from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.models import FamilyInvite, OTPVerification, User
from core.throttles import OtpSendThrottle
from core.phone_auth import find_user_by_phone_input, normalize_nepal_phone
from core.services import otp_service
from core.services.base import get_or_create_personal_wallet
from core.portal_roles import (
    PORTAL_ADMIN,
    PORTAL_CHILD,
    PORTAL_FAMILY,
    PORTAL_MAIN,
    PORTAL_VENDOR,
    normalize_portal_key,
    user_allowed_for_portal_key,
)
from core.services.site_settings_policy import new_account_gate_response
from core.views.unified_auth import (
    build_auth_response_for_portal,
    build_auth_success_response,
)


def _resolve_signup_referrer(request_data, signup_phone_norm: str):
    """Resolve optional referrer for OTP signup. Returns (User|None, Response|None error)."""
    from rest_framework.response import Response

    from core.phone_auth import normalize_nepal_phone

    rid_raw = request_data.get("referrer_id")
    kid = (request_data.get("referrer_kid") or "").strip()
    ref = (request_data.get("ref") or "").strip()

    referrer = None

    if rid_raw is not None and str(rid_raw).strip() != "":
        try:
            pk = int(rid_raw)
        except (TypeError, ValueError):
            return None, Response({"detail": "Invalid referrer_id."}, status=400)
        referrer = User.objects.filter(pk=pk).first()
        if not referrer:
            return None, Response({"detail": "Invalid referrer."}, status=400)
    elif kid:
        referrer = User.objects.filter(KID__iexact=kid).first()
        if not referrer:
            return None, Response({"detail": "Invalid referral code."}, status=400)
    elif ref:
        if ref.isdigit():
            referrer = User.objects.filter(pk=int(ref)).first()
        if not referrer:
            referrer = User.objects.filter(KID__iexact=ref).first()
        if not referrer:
            return None, Response({"detail": "Invalid referral code."}, status=400)
    else:
        return None, None

    if not referrer.is_active:
        return None, Response({"detail": "Invalid referrer."}, status=400)
    if normalize_nepal_phone(referrer.phone or "") == signup_phone_norm:
        return None, Response({"detail": "You cannot refer yourself."}, status=400)
    return referrer, None


def _username_from_name(name: str) -> str:
    from django.utils.text import slugify
    import secrets

    base = slugify(name.replace(" ", "_"))[:36] or "user"
    for _ in range(30):
        candidate = f"{base}_{secrets.token_hex(3)}"
        if not User.objects.filter(username=candidate).exists():
            return candidate
    return f"{base}_{secrets.token_hex(8)}"


def _fallback_portal_key_for_user(user: User) -> str | None:
    """
    Use a best-effort portal fallback when generic storefront login was requested.
    Priority: admin, vendor, family, child, customer (matches primary_spa_redirect).
    """
    for key in (PORTAL_ADMIN, PORTAL_VENDOR, PORTAL_FAMILY, PORTAL_CHILD, PORTAL_MAIN):
        if user_allowed_for_portal_key(user, key):
            return key
    return None


@api_view(["POST"])
@throttle_classes([OtpSendThrottle])
@permission_classes([AllowAny])
def otp_send(request):
    purpose = (request.data.get("purpose") or "").strip().lower()
    name = (request.data.get("name") or "").strip()

    if purpose == OTPVerification.Purpose.ADMIN_SENSITIVE:
        return Response({"detail": "Invalid purpose."}, status=400)

    raw_phone = (request.data.get("phone") or "").strip()
    phone = normalize_nepal_phone(raw_phone)
    if not phone:
        return Response({"detail": "Enter a valid Nepal mobile number."}, status=400)

    if purpose == OTPVerification.Purpose.FAMILY_INVITE:
        invite_token = (request.data.get("invite_token") or "").strip()
        if not invite_token:
            return Response({"detail": "invite_token is required."}, status=400)
        invite = FamilyInvite.objects.filter(token=invite_token).first()
        if not invite:
            return Response({"detail": "Invalid invite."}, status=400)
        if invite.status != FamilyInvite.Status.PENDING:
            return Response({"detail": "This invite is no longer valid."}, status=400)
        if invite.expires_at < timezone.now():
            return Response({"detail": "This invite has expired."}, status=400)
        if invite.phone != phone:
            return Response({"detail": "Phone does not match this invite."}, status=400)
        try:
            otp_service.create_otp(phone, purpose, signup_name="")
        except otp_service.OTPError as e:
            return Response({"detail": str(e)}, status=400)
        payload = {"detail": "OTP sent."}
        if settings.DEBUG:
            latest = (
                OTPVerification.objects.filter(phone=phone, purpose=purpose, is_used=False)
                .order_by("-created_at")
                .first()
            )
            if latest:
                payload["debug_otp"] = latest.otp
        return Response(payload)

    if purpose not in (OTPVerification.Purpose.LOGIN, OTPVerification.Purpose.SIGNUP):
        return Response({"detail": "Invalid purpose."}, status=400)

    if purpose == OTPVerification.Purpose.LOGIN:
        user = find_user_by_phone_input(phone)
        if not user:
            return Response({"detail": "No account found for this number."}, status=400)
        if not user.is_active:
            return Response({"detail": "This account is disabled."}, status=400)
    else:
        if not name:
            return Response({"detail": "Name is required."}, status=400)
        if find_user_by_phone_input(phone):
            return Response({"detail": "An account with this number already exists."}, status=400)
        gate = new_account_gate_response()
        if gate:
            return gate
        signup_portal = normalize_portal_key(
            request.data.get("portal") or request.data.get("surface")
        )
        if signup_portal == PORTAL_CHILD:
            return Response(
                {
                    "detail": "Child accounts are created when a parent adds you or sends an invite. "
                    "Use the link or phone number from your family invitation.",
                },
                status=400,
            )
        if signup_portal and signup_portal not in (PORTAL_MAIN, None):
            return Response(
                {
                    "detail": "Sign up creates a normal customer account. "
                    "After sign-in, use Switch Portal to set up parent or child access.",
                },
                status=400,
            )

    try:
        otp_service.create_otp(
            phone,
            purpose,
            signup_name=name if purpose == OTPVerification.Purpose.SIGNUP else "",
        )
    except otp_service.OTPError as e:
        return Response({"detail": str(e)}, status=400)

    payload = {"detail": "OTP sent."}
    if settings.DEBUG:
        latest = (
            OTPVerification.objects.filter(phone=phone, purpose=purpose, is_used=False)
            .order_by("-created_at")
            .first()
        )
        if latest:
            payload["debug_otp"] = latest.otp
    return Response(payload)


@api_view(["POST"])
@permission_classes([AllowAny])
def otp_verify(request):
    raw_phone = (request.data.get("phone") or "").strip()
    code = (request.data.get("otp") or "").strip()
    purpose = (request.data.get("purpose") or "").strip().lower()
    name = (request.data.get("name") or "").strip()

    if purpose not in (OTPVerification.Purpose.LOGIN, OTPVerification.Purpose.SIGNUP):
        return Response({"detail": "Invalid purpose."}, status=400)

    phone = normalize_nepal_phone(raw_phone)
    if not phone or len(code) != 6 or not code.isdigit():
        return Response({"detail": "Invalid phone or OTP."}, status=400)

    try:
        row = otp_service.consume(phone, purpose, code)
    except otp_service.OTPError as e:
        return Response({"detail": str(e)}, status=400)

    if purpose == OTPVerification.Purpose.LOGIN:
        user = find_user_by_phone_input(phone)
        if not user or not user.is_active:
            return Response({"detail": "Account not available."}, status=400)
        portal_key = normalize_portal_key(
            request.data.get("portal") or request.data.get("surface")
        )
        if not portal_key:
            return Response(
                {
                    "detail": "Specify which portal you are signing in to: "
                    '"portal", "family-portal", "child-portal", "vendor", or "admin".',
                    "code": "portal_required",
                },
                status=400,
            )
        out = build_auth_response_for_portal(user, portal_key)
        if isinstance(out, Response):
            # For generic customer login, avoid hard-failing with 403 when the account
            # belongs to another allowed portal (family/child/vendor/admin).
            if out.status_code == 403 and portal_key == PORTAL_MAIN:
                fallback_key = _fallback_portal_key_for_user(user)
                if fallback_key:
                    return Response(build_auth_success_response(user, fallback_key))
            return out
        return Response(out)

    signup_name = (row.signup_name or name or "").strip()
    if not signup_name:
        return Response({"detail": "Name is required."}, status=400)
    if find_user_by_phone_input(phone):
        return Response({"detail": "An account with this number already exists."}, status=400)

    gate = new_account_gate_response()
    if gate:
        return gate

    portal_key = PORTAL_MAIN
    signup_portal = normalize_portal_key(
        request.data.get("portal") or request.data.get("surface")
    )
    if signup_portal == PORTAL_CHILD:
        return Response(
            {
                "detail": "Child accounts are created when a parent adds you or sends an invite. "
                "Use the link or phone number from your family invitation.",
            },
            status=400,
        )
    if signup_portal and signup_portal not in (PORTAL_MAIN, None):
        return Response(
            {
                "detail": "Sign up creates a normal customer account. "
                "After sign-in, use Switch Portal to set up parent or child access.",
            },
            status=400,
        )

    referrer, ref_err = _resolve_signup_referrer(request.data, phone)
    if ref_err:
        return ref_err
    try:
        with transaction.atomic():
            user = User(
                name=signup_name,
                phone=phone,
                username=_username_from_name(signup_name),
                role=User.Role.NORMAL,
                referred_by=referrer,
            )
            user.set_unusable_password()
            user.save()
            get_or_create_personal_wallet(user)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    out = build_auth_response_for_portal(user, portal_key)
    if isinstance(out, Response):
        return out
    return Response(out)
