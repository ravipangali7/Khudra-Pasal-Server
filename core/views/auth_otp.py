from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.models import FamilyInvite, OTPVerification, User
from core.phone_auth import find_user_by_phone_input, normalize_nepal_phone
from core.services import family_service, otp_service
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
from core.views.unified_auth import (
    build_auth_response_for_portal,
    build_auth_success_response,
)


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
@permission_classes([AllowAny])
def otp_send(request):
    raw_phone = (request.data.get("phone") or "").strip()
    purpose = (request.data.get("purpose") or "").strip().lower()
    name = (request.data.get("name") or "").strip()

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
        otp_service.create_otp(phone, purpose, signup_name="")
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
        if signup_portal and signup_portal not in (PORTAL_MAIN, PORTAL_FAMILY):
            return Response(
                {
                    "detail": "Sign up with portal \"portal\" (customer) or \"family-portal\" (family head).",
                },
                status=400,
            )

    otp_service.create_otp(phone, purpose, signup_name=name if purpose == OTPVerification.Purpose.SIGNUP else "")

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

    portal_key = normalize_portal_key(
        request.data.get("portal") or request.data.get("surface")
    ) or PORTAL_MAIN
    if portal_key == PORTAL_CHILD:
        return Response(
            {
                "detail": "Child accounts are created when a parent adds you or sends an invite. "
                "Use the link or phone number from your family invitation.",
            },
            status=400,
        )
    if portal_key not in (PORTAL_MAIN, PORTAL_FAMILY):
        return Response(
            {
                "detail": "Sign up with portal \"portal\" (customer) or \"family-portal\" (family head).",
            },
            status=400,
        )

    family_name = (request.data.get("family_name") or "").strip()
    try:
        with transaction.atomic():
            user = User(
                name=signup_name,
                phone=phone,
                username=_username_from_name(signup_name),
                role=User.Role.NORMAL,
            )
            user.set_unusable_password()
            user.save()
            if portal_key == PORTAL_FAMILY:
                gname = family_name[:100] if family_name else f"{user.name}'s Family"
                family_service.create_family_group_for_user(user, gname)
            get_or_create_personal_wallet(user)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)

    out = build_auth_response_for_portal(user, portal_key)
    if isinstance(out, Response):
        return out
    return Response(out)
