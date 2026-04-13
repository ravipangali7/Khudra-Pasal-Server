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
from rest_framework_simplejwt.tokens import RefreshToken

from core.services.base import get_or_create_personal_wallet
from core.views.social_oauth import _get_or_create_social_user, sign_oauth_pending_token


class GoogleCredentialLoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        credential = request.data.get("access_token") or request.data.get("id_token")
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
                get_or_create_personal_wallet(user)

        if not user.is_active:
            return Response(
                {"detail": "Account disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not user.oauth_phone_completed:
            next_path = (getattr(settings, "REDIRECT_AFTER_LOGIN", "/portal") or "/portal").strip() or "/portal"
            return Response(
                {
                    "requires_oauth_phone": True,
                    "pending_token": sign_oauth_pending_token(user.pk, next_path),
                },
                status=status.HTTP_200_OK,
            )

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": {
                    "pk": user.pk,
                    "email": user.email,
                    "name": user.name,
                },
            },
            status=status.HTTP_200_OK,
        )
