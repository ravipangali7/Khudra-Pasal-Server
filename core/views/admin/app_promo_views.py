from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import AppPromotionAttribution
from core.services.app_promotion_attribution import attribution_admin_row
from core.views.admin.user_views import _forbidden_if_not_admin


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_app_promotion_attributions_list(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    status = (request.query_params.get("status") or "").strip()
    qs = AppPromotionAttribution.objects.select_related("user", "first_order").order_by(
        "-clicked_at"
    )
    if status:
        qs = qs.filter(status=status)
    search = (request.query_params.get("search") or "").strip()
    if search:
        from django.db.models import Q

        qs = qs.filter(
            Q(user__name__icontains=search)
            | Q(user__phone__icontains=search)
            | Q(visit_token__icontains=search)
            | Q(banner_headline__icontains=search)
        )
    limit = min(int(request.query_params.get("limit") or 100), 500)
    rows = [attribution_admin_row(a) for a in qs[:limit]]
    clicked = AppPromotionAttribution.objects.filter(
        status=AppPromotionAttribution.Status.CLICKED
    ).count()
    installed = AppPromotionAttribution.objects.filter(
        status=AppPromotionAttribution.Status.INSTALLED
    ).count()
    redeemed = AppPromotionAttribution.objects.filter(
        status=AppPromotionAttribution.Status.REDEEMED
    ).count()
    return Response(
        {
            "results": rows,
            "summary": {
                "clicked": clicked,
                "installed": installed,
                "redeemed": redeemed,
                "total": AppPromotionAttribution.objects.count(),
            },
        }
    )
