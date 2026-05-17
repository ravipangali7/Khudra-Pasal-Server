from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.services.app_promotion_attribution import (
    VISIT_TOKEN_COOKIE,
    claim_app_install,
    record_banner_click,
    visit_token_from_request,
)


@api_view(["POST"])
@permission_classes([AllowAny])
def app_promotion_banner_click(request):
    """Record banner CTA click; returns visit_token for localStorage + Play Store referrer."""
    user = request.user if request.user.is_authenticated else None
    body_token = ""
    if isinstance(request.data, dict):
        body_token = str(request.data.get("visit_token") or "").strip()
    token = body_token or visit_token_from_request(request)
    out = record_banner_click(request, user=user, visit_token=token or None)
    if not out.get("ok"):
        return Response(out, status=400)
    resp = Response(out)
    resp.set_cookie(
        VISIT_TOKEN_COOKIE,
        out["visit_token"],
        max_age=60 * 60 * 24 * 90,
        samesite="Lax",
        secure=request.is_secure(),
        httponly=False,
    )
    return resp


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def app_promotion_claim_install(request):
    """Mark install claimed (native app first session or explicit claim)."""
    body_token = ""
    if isinstance(request.data, dict):
        body_token = str(request.data.get("visit_token") or "").strip()
    token = body_token or visit_token_from_request(request)
    out = claim_app_install(request.user, visit_token=token or None)
    status = 200 if out.get("ok") else 400
    return Response(out, status=status)
