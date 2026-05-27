"""Public eSewa return URLs for POS checkout."""

from __future__ import annotations

from django.http import HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from core.services import pos_payment_service


@csrf_exempt
@require_http_methods(["GET", "POST"])
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def pos_esewa_success(request):
    url = pos_payment_service.handle_esewa_pos_callback(request, success=True)
    return HttpResponseRedirect(url)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def pos_esewa_failure(request):
    url = pos_payment_service.handle_esewa_pos_callback(request, success=False)
    return HttpResponseRedirect(url)
