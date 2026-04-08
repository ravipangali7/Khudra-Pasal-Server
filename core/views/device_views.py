"""Device registration (FCM web token) for authenticated users."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.views.admin.admin_write_utils import validation_error

_FCM_TOKEN_MAX_LEN = 8192


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
    User = request.user.__class__
    User.objects.filter(pk=request.user.pk).update(fcm_token=token)
    return Response({"ok": True})
