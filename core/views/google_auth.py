from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.portal_roles import infer_portal_key_from_frontend_path
from core.services import family_service
from core.services.base import get_or_create_personal_wallet
from core.views.social_oauth import (
    _effective_portal_key_for_oauth,
    _get_or_create_social_user,
    sign_oauth_pending_token,
)
from core.views.unified_auth import build_auth_response_for_portal


class GoogleCredentialLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        credential = request.data.get("access_token") or request.data.get("id_token")
        flow = str(request.data.get("flow") or "login").strip().lower()
        if flow not in ("login", "register"):
            flow = "login"
        next_path = (
            str(request.data.get("next") or "").strip()
            or (getattr(settings, "REDIRECT_AFTER_LOGIN", "/portal") or "/portal").strip()
            or "/portal"
        )
        if not credential or not isinstance(credential, str):
            return Response(
                {"detail": "Missing Google credential token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        audience = (getattr(settings, "GOOGLE_ID_TOKEN_AUDIENCE", "") or "").strip()
        if not audience:
            return Response(
                {"detail": "GOOGLE_ID_TOKEN_AUDIENCE is not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        try:
            payload = id_token.verify_oauth2_token(
                credential, google_requests.Request(), audience=audience
            )
        except ValueError:
            return Response(
                {"detail": "Invalid Google credential token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if payload.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            return Response(
                {"detail": "Invalid Google token issuer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        provider_user_id = str(payload.get("sub") or "")
        if not provider_user_id:
            return Response(
                {"detail": "Google token missing subject."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        name = str(payload.get("name") or payload.get("email") or "Google User")[:150]
        email = str(payload.get("email") or "")[:254]
        picture = str(payload.get("picture") or "")

        with transaction.atomic():
            user, created = _get_or_create_social_user(
                "google", provider_user_id, name, email, avatar_url=picture
            )
            if created:
                if flow == "login":
                    return Response(
                        {"detail": "No account found for this Google login. Please sign up first."},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                get_or_create_personal_wallet(user)
                if infer_portal_key_from_frontend_path(next_path) == "family-portal":
                    try:
                        family_service.create_family_group_for_user(user, f"{user.name}'s Family")
                    except ValueError as e:
                        return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            elif flow == "register":
                return Response(
                    {"detail": "An account already exists for this Google identity. Please log in."},
                    status=status.HTTP_409_CONFLICT,
                )

        if not user.is_active:
            return Response(
                {"detail": "Account disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.oauth_phone_completed:
            return Response(
                {
                    "requires_oauth_phone": True,
                    "pending_token": sign_oauth_pending_token(user.pk, next_path),
                },
                status=status.HTTP_200_OK,
            )

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        requested_portal = infer_portal_key_from_frontend_path(next_path)
        portal_key = _effective_portal_key_for_oauth(user, next_path)
        out = build_auth_response_for_portal(user, portal_key)
        if isinstance(out, Response):
            detail = getattr(out, "data", None) or {}
            msg = detail.get("detail", "Sign-in not allowed for this portal.")
            return Response({"detail": str(msg)}, status=out.status_code)
        redirect_final = out["redirect"]
        if portal_key == requested_portal and next_path.startswith("/") and not next_path.startswith("//"):
            legacy_dashboard = next_path.strip() == "/portal/dashboard"
            canonical = str(redirect_final or "").rstrip("/") == "/portal"
            if not (legacy_dashboard and canonical):
                redirect_final = next_path
        out["redirect"] = redirect_final
        return Response(
            out,
            status=status.HTTP_200_OK,
        )
