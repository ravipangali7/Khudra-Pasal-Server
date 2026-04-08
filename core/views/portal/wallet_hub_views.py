"""Cross-portal wallet transfer by globally unique transfer code (+ optional QR)."""

from __future__ import annotations

from collections.abc import Mapping

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    parser_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from core.models import (
    FamilyMember,
    OTPVerification,
    User,
    Wallet,
    WalletTransaction,
    WalletTransferCode,
    WalletTransferIdempotency,
)
from core.services import wallet_policy, wallet_service
from core.services.wallet_transfer_code_service import (
    generate_transfer_code,
    get_or_create_transfer_code_row,
    mask_display_name,
    normalize_transfer_code,
    resolve_recipient_wallet,
    resolve_sender_wallet_for_hub_transfer,
)
from core.throttles import WalletHubTransferCodeLookupThrottle
from core.views.admin.admin_write_utils import absolute_media_url, validation_error
from core.views.admin.resource_views import _to_decimal
from core.views.portal.portal_views import _portal_consume_otp_or_error


class IsPortalWalletHubUser(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        if not u or not u.is_authenticated:
            return False
        if u.role == User.Role.NORMAL:
            return True
        if u.role == User.Role.PARENT:
            return True
        if u.role == User.Role.CHILD:
            if getattr(settings, "CHILD_PORTAL_REQUIRE_MEMBERSHIP", False):
                return FamilyMember.objects.filter(
                    user=u,
                    role=FamilyMember.Role.CHILD,
                    status=FamilyMember.Status.ACTIVE,
                ).exists()
            return True
        return False


def _serialize_transfer_code(request, row: WalletTransferCode) -> dict:
    return {
        "code": row.code,
        "qr_image_url": (
            absolute_media_url(request, row.qr_image) if row.qr_image else ""
        ),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalWalletHubUser])
def wallet_hub_transfer_id_me(request):
    row = WalletTransferCode.objects.filter(user=request.user).first()
    if not row:
        return Response(
            {"detail": "No transfer ID yet. Create one with POST …/transfer-id/create."},
            status=404,
        )
    return Response(_serialize_transfer_code(request, row))


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalWalletHubUser])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def wallet_hub_transfer_id_create(request):
    user = request.user
    data = request.data if isinstance(request.data, Mapping) else {}
    want_regenerate = str(data.get("regenerate") or "").lower() in (
        "1",
        "true",
        "yes",
    )

    row, created = get_or_create_transfer_code_row(user)
    if want_regenerate:
        for _ in range(64):
            new_code = generate_transfer_code()
            if WalletTransferCode.objects.filter(code=new_code).exclude(pk=row.pk).exists():
                continue
            row.code = new_code
            break
        else:
            return Response({"detail": "Could not allocate a unique code."}, status=500)

    f = request.FILES.get("qr_image")
    if f:
        row.qr_image = f
    row.save()
    return Response(_serialize_transfer_code(request, row), status=201 if created else 200)


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalWalletHubUser])
@throttle_classes([WalletHubTransferCodeLookupThrottle])
def wallet_hub_transfer_id_lookup(request, code: str):
    norm = normalize_transfer_code(code)
    if len(norm) < 6:
        return Response({"detail": "Invalid transfer ID."}, status=404)
    row = WalletTransferCode.objects.select_related("user").filter(code=norm).first()
    if not row:
        return Response({"detail": "Transfer ID not found."}, status=404)
    u = row.user
    avatar_url = ""
    if u.avatar:
        avatar_url = absolute_media_url(request, u.avatar) or ""
    return Response(
        {
            "code": row.code,
            "display_name": mask_display_name(u.name),
            "avatar_url": avatar_url,
        }
    )


def _idem_cached_response(row: WalletTransferIdempotency) -> Response | None:
    if row.status not in (
        WalletTransferIdempotency.Status.COMPLETED,
        WalletTransferIdempotency.Status.FAILED,
    ) or not row.cached_response:
        return None
    body = dict(row.cached_response)
    st = int(body.pop("_http_status", 200))
    return Response(body, status=st)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated, IsPortalWalletHubUser])
def wallet_hub_wallet_transfer(request):
    user = request.user
    data = request.data if isinstance(request.data, Mapping) else {}
    raw_code = (
        (data.get("transfer_id") or data.get("transfer_code") or "")
    )
    norm = normalize_transfer_code(str(raw_code))
    amount = _to_decimal(data.get("amount"), "0")
    client_key = (
        (request.headers.get("Idempotency-Key") or "").strip()
        or str(data.get("client_ref") or "").strip()
    )

    if len(client_key) < 8 or len(client_key) > 128:
        return validation_error(
            "Provide Idempotency-Key header or client_ref (8–128 characters).",
            field="client_ref",
        )
    if amount <= 0:
        return validation_error("amount must be positive", field="amount")
    if len(norm) < 6:
        return validation_error("transfer_id required", field="transfer_id")

    cached = WalletTransferIdempotency.objects.filter(
        sender=user, client_key=client_key
    ).first()
    if cached and cached.status in (
        WalletTransferIdempotency.Status.COMPLETED,
        WalletTransferIdempotency.Status.FAILED,
    ):
        r = _idem_cached_response(cached)
        if r is not None:
            return r

    with transaction.atomic():
        try:
            row = WalletTransferIdempotency.objects.create(
                sender=user,
                client_key=client_key,
                status=WalletTransferIdempotency.Status.PENDING,
            )
        except IntegrityError:
            row = (
                WalletTransferIdempotency.objects.select_for_update()
                .filter(sender=user, client_key=client_key)
                .first()
            )
            if not row:
                return Response({"detail": "Idempotency conflict."}, status=409)
            if row.status in (
                WalletTransferIdempotency.Status.COMPLETED,
                WalletTransferIdempotency.Status.FAILED,
            ):
                r = _idem_cached_response(row)
                if r is not None:
                    return r

        row = WalletTransferIdempotency.objects.select_for_update().get(pk=row.pk)
        if row.status in (
            WalletTransferIdempotency.Status.COMPLETED,
            WalletTransferIdempotency.Status.FAILED,
        ):
            r = _idem_cached_response(row)
            if r is not None:
                return r

        code_row = (
            WalletTransferCode.objects.select_related("user")
            .filter(code=norm)
            .first()
        )
        if not code_row or code_row.user_id == user.pk:
            err = validation_error(
                "Invalid transfer ID or cannot transfer to yourself.",
                field="transfer_id",
            )
            row.status = WalletTransferIdempotency.Status.FAILED
            row.cached_response = {
                **(err.data if hasattr(err, "data") else {"detail": "error"}),
                "_http_status": err.status_code,
            }
            row.save(update_fields=["status", "cached_response", "updated_at"])
            return err

        recipient = code_row.user
        from_w = resolve_sender_wallet_for_hub_transfer(user)
        if not from_w:
            body = {"detail": "No outbound wallet available for your account."}
            row.status = WalletTransferIdempotency.Status.FAILED
            row.cached_response = {**body, "_http_status": 400}
            row.save(update_fields=["status", "cached_response", "updated_at"])
            return Response(body, status=400)

        if from_w.status != Wallet.Status.ACTIVE:
            body = {"detail": "Your wallet is not active."}
            row.status = WalletTransferIdempotency.Status.FAILED
            row.cached_response = {**body, "_http_status": 400}
            row.save(update_fields=["status", "cached_response", "updated_at"])
            return Response(body, status=400)

        to_w = resolve_recipient_wallet(recipient)
        if to_w.status != Wallet.Status.ACTIVE:
            body = {"detail": "Recipient wallet is not active."}
            row.status = WalletTransferIdempotency.Status.FAILED
            row.cached_response = {**body, "_http_status": 400}
            row.save(update_fields=["status", "cached_response", "updated_at"])
            return Response(body, status=400)

        try:
            wallet_policy.assert_hub_transfer_allowed(from_w, to_w)
            wallet_policy.assert_may_credit_wallet(to_w, amount)
            wallet_policy.assert_daily_transfer_for_wallet(from_w, amount)
            if wallet_policy.transfer_requires_otp(amount):
                otp_resp = _portal_consume_otp_or_error(
                    request, str(OTPVerification.Purpose.TRANSFER)
                )
                if otp_resp is not None:
                    WalletTransferIdempotency.objects.filter(pk=row.pk).delete()
                    return otp_resp

            fee = wallet_policy.compute_peer_transfer_fee(amount)
            txn_status = (
                WalletTransaction.Status.FLAGGED
                if wallet_policy.transfer_should_auto_flag(amount)
                else WalletTransaction.Status.COMPLETED
            )
            out_t, _in_t = wallet_service.execute_transfer(
                from_w,
                to_w,
                amount,
                performed_by=user,
                platform_fee=fee,
                txn_status=txn_status,
                reference_type="hub_transfer_code",
            )
            from_w.refresh_from_db()
            ok_body = {
                "ok": True,
                "balance": float(from_w.balance),
                "outbound_txn_id": out_t.txn_id,
                "_http_status": 200,
            }
            row.status = WalletTransferIdempotency.Status.COMPLETED
            row.outbound_txn_id = out_t.txn_id
            row.cached_response = dict(ok_body)
            row.save(
                update_fields=[
                    "status",
                    "outbound_txn_id",
                    "cached_response",
                    "updated_at",
                ]
            )
            return Response(
                {
                    "ok": True,
                    "balance": float(from_w.balance),
                    "outbound_txn_id": out_t.txn_id,
                },
                status=200,
            )
        except ValueError as e:
            msg = str(e)
            body = {"detail": msg}
            row.status = WalletTransferIdempotency.Status.FAILED
            row.cached_response = {**body, "_http_status": 400}
            row.save(update_fields=["status", "cached_response", "updated_at"])
            return Response(body, status=400)
