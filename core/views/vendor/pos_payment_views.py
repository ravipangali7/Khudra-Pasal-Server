"""Vendor POS payment session endpoints."""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.services import pos_payment_service
from core.services.site_settings_policy import (
    pos_checkout_allowed,
    pos_disabled_response,
    vendor_pos_checkout_allowed,
    vendor_pos_disabled_response,
)
from core.views.admin.resource_views import validation_error
from core.views.vendor.common import vendor_or_error


def _cart_payload_from_request(request) -> dict:
    items = request.data.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    return {
        "items": items,
        "customer_id": request.data.get("customer_id"),
        "tax_percent": str(request.data.get("tax_percent") or "0"),
        "discount": str(request.data.get("discount") or "0"),
        "notes": (request.data.get("notes") or "")[:500],
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_payment_methods_status(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    return Response(pos_payment_service.pos_payment_methods_status())


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_nchl_qr_session_create(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if not pos_checkout_allowed():
        return pos_disabled_response()
    if not vendor_pos_checkout_allowed(vendor):
        return vendor_pos_disabled_response()
    try:
        cart = _cart_payload_from_request(request)
    except ValueError as e:
        return validation_error(str(e), field="items")
    try:
        session = pos_payment_service.create_nchl_qr_session(
            request=request,
            created_by=request.user,
            acting_vendor=vendor,
            cart_payload=cart,
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(pos_payment_service._session_to_dict(session), status=201)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_payment_session_detail(request, session_id: str):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    try:
        session = pos_payment_service.get_session_for_user(session_id, request.user, vendor=vendor)
        session = pos_payment_service.refresh_session_status(session)
    except ValueError as e:
        return Response({"detail": str(e)}, status=404)
    return Response(pos_payment_service._session_to_dict(session))


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_nchl_qr_session_confirm_demo(request, session_id: str):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    try:
        session = pos_payment_service.get_session_for_user(session_id, request.user, vendor=vendor)
        order = pos_payment_service.confirm_demo_nchl_payment(session)
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response(
        {
            **pos_payment_service._session_to_dict(session),
            "order_number": order.order_number,
            "total": float(order.total),
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def vendor_pos_esewa_session_create(request):
    vendor, err = vendor_or_error(request)
    if err:
        return err
    if not pos_checkout_allowed():
        return pos_disabled_response()
    if not vendor_pos_checkout_allowed(vendor):
        return vendor_pos_disabled_response()
    try:
        cart = _cart_payload_from_request(request)
    except ValueError as e:
        return validation_error(str(e), field="items")
    try:
        session, redirect = pos_payment_service.create_esewa_pos_session(
            request=request,
            created_by=request.user,
            acting_vendor=vendor,
            cart_payload=cart,
            success_reverse_name="pos-esewa-success",
            failure_reverse_name="pos-esewa-failure",
        )
    except ValueError as e:
        return Response({"detail": str(e)}, status=400)
    return Response({**pos_payment_service._session_to_dict(session), **redirect}, status=201)
