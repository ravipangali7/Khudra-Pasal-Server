"""Device registration (FCM tokens) for authenticated users."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.services.fcm_device_service import _FCM_TOKEN_MAX_LEN, register_user_fcm_token
from core.views.admin.admin_write_utils import validation_error


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def auth_fcm_token(request):
    raw = request.data.get("fcm_token")
    if raw is None:
        return validation_error("fcm_token is required", field="fcm_token")
    if not isinstance(raw, str):
        return validation_error("fcm_token must be a string", field="fcm_token")
    token = raw.strip()
    if len(token) > _FCM_TOKEN_MAX_LEN:
        return validation_error("fcm_token is too long", field="fcm_token")
    if not token:
        return validation_error("fcm_token cannot be empty", field="fcm_token")

    platform = request.data.get("platform")
    if platform is not None and not isinstance(platform, str):
        return validation_error("platform must be a string", field="platform")

    device = register_user_fcm_token(
        request.user,
        token,
        platform=(platform or "").strip() if isinstance(platform, str) else "",
    )
    if device is None:
        return validation_error("fcm_token is invalid", field="fcm_token")

    return Response({"ok": True, "updated": True})
