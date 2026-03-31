from rest_framework import status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.models import User
from core.serializers import (
    FamilyJoinRequestReadSerializer,
    PublicFamilyPortalJoinSubmitSerializer,
)
from core.services import family_portal_join_link_service
from core.throttles import FamilyPortalJoinThrottle

_JOIN_APPLICANT_ROLES = frozenset(
    {User.Role.NORMAL, User.Role.PARENT, User.Role.CHILD},
)


def _public_link_not_found():
    return Response({"detail": "Invalid or expired invitation link."}, status=404)


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([AllowAny])
@throttle_classes([FamilyPortalJoinThrottle])
def website_family_portal_join(request, token: str):
    link = family_portal_join_link_service.resolve_public_link(token.strip())
    if not link:
        return _public_link_not_found()

    if request.method == "GET":
        return Response(
            {
                "ok": True,
                "title": link.title or "",
                "welcome_message": link.welcome_message or "",
                "group_name": (link.group.name or "").strip() or "Family",
                "expires_at": link.expires_at.isoformat() if link.expires_at else None,
                "fields": {
                    "name": {"required": True},
                    "email": {"required": False},
                    "phone": {"required": True},
                    "applicant_note": {"required": False},
                },
            }
        )

    if not request.user.is_authenticated:
        return Response(
            {"detail": "Authentication credentials were not provided."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if request.user.role not in _JOIN_APPLICANT_ROLES:
        return Response(
            {
                "detail": "Sign in with a customer account to request joining a family.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    ser = PublicFamilyPortalJoinSubmitSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=400)
    v = ser.validated_data
    try:
        jr = family_portal_join_link_service.submit_join_application(
            link=link,
            applicant_user=request.user,
            name=v["name"],
            email=v.get("email") or "",
            phone=v["phone"],
            applicant_note=v.get("applicant_note") or "",
        )
    except ValueError as e:
        msg = str(e)
        if "already exists" in msg.lower():
            return Response({"detail": msg}, status=409)
        return Response({"detail": msg}, status=400)

    return Response(
        {
            "ok": True,
            "message": "Your request has been submitted. The family organizer will review it.",
            "join_request": FamilyJoinRequestReadSerializer(jr).data,
        },
        status=status.HTTP_201_CREATED,
    )
