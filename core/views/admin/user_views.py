import os
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.fields import DecimalField
from django.db.models.functions import Coalesce
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.models import (
    AuditLog,
    EmployeeProfile,
    FamilyGroup,
    FamilyMember,
    KYCDocument,
    Order,
    SecuritySettings,
    User,
    Vendor,
    Wallet,
)
from core.phone_auth import (
    authenticate_user_by_email,
    authenticate_user_by_phone,
    find_user_by_email,
    find_user_by_phone_input,
    normalize_nepal_phone,
)
from core.throttles import AdminLoginThrottle
from core.portal_roles import PORTAL_ADMIN, assert_portal_login_allowed, user_allowed_for_admin_portal
from core.services import audit_service, security_service
from core.services.kyc_portal import supersede_non_approved_kyc, validate_kyc_upload_file
from core.services.kyc_service import sync_user_kyc_status
from core import rbac_django as rbac
from core.serializers import AdminUserSerializer
from core.views.admin.admin_write_utils import absolute_media_url, client_ip_from_request, validation_error


def _annotate_admin_user_customer_metrics(queryset):
    """Subquery aggregates so list rows do not double-count via joins."""
    order_count_sq = (
        Order.objects.filter(customer_id=OuterRef("pk"))
        .values("customer_id")
        .annotate(_c=Count("id"))
        .values("_c")[:1]
    )
    # Lifetime order volume: sum order totals for every row tied to this customer
    # (all statuses — pending through delivered — so admin "Total spend" matches placed orders).
    spent_sq = (
        Order.objects.filter(customer_id=OuterRef("pk"))
        .values("customer_id")
        .annotate(_s=Sum("total"))
        .values("_s")[:1]
    )
    wallet_sq = (
        Wallet.objects.filter(owner_id=OuterRef("pk"))
        .exclude(type=Wallet.Type.VENDOR)
        .values("owner_id")
        .annotate(_w=Sum("balance"))
        .values("_w")[:1]
    )
    return queryset.annotate(
        admin_order_count=Coalesce(
            Subquery(order_count_sq, output_field=IntegerField()),
            Value(0),
        ),
        admin_total_spent=Coalesce(
            Subquery(
                spent_sq,
                output_field=DecimalField(max_digits=14, decimal_places=2),
            ),
            Value(Decimal("0")),
        ),
        admin_wallet_balance=Coalesce(
            Subquery(
                wallet_sq,
                output_field=DecimalField(max_digits=16, decimal_places=2),
            ),
            Value(Decimal("0")),
        ),
    )


def _validate_customer_document_file(uploaded):
    ext = os.path.splitext(uploaded.name)[1].lower().lstrip(".")
    allowed = getattr(
        settings,
        "CUSTOMER_DOCUMENT_ALLOWED_EXTENSIONS",
        ("pdf", "png", "jpg", "jpeg", "webp"),
    )
    if ext not in allowed:
        return validation_error(
            f"document must be one of: {', '.join(sorted(allowed))}",
            field="customer_document",
        )
    return None


def _forbidden_if_not_admin(request):
    from core.views.admin.admin_access import enforce_admin_api_access

    return enforce_admin_api_access(request)


def _admin_user_delete_forbidden(request, target: User) -> str | None:
    """Return an error message if the actor may not delete target, else None."""
    from core.views.admin.admin_access import user_can_access_audit_logs

    actor = request.user
    if not user_can_access_audit_logs(actor):
        return "Delete permission requires super admin privileges."
    if target.pk == actor.pk:
        return "You cannot delete your own account."
    if getattr(target, "is_superuser", False):
        return "Superuser accounts cannot be deleted."
    if target.role == User.Role.SUPER_ADMIN:
        return "Super admin accounts cannot be deleted."
    if Vendor.objects.filter(user_id=target.pk).exists():
        return "Vendor accounts must be managed from the Sellers section."
    return None


class UserPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


ADMIN_LOGIN_FAIL_PREFIX = "admin_login_fail:"
ADMIN_LOGIN_FAIL_TTL = 1800
ADMIN_LOGIN_FAIL_MAX = int(os.environ.get("ADMIN_LOGIN_FAIL_MAX", "5"))


def _admin_login_fail_cache_key(identifier: str, *, use_email: bool) -> str:
    if use_email:
        ident = (identifier or "").strip().lower()[:254] or "unknown"
        return f"{ADMIN_LOGIN_FAIL_PREFIX}email:{ident}"
    n = normalize_nepal_phone(identifier)
    ident = n or "".join(c for c in identifier if c.isdigit())[-12:] or identifier[:24]
    return f"{ADMIN_LOGIN_FAIL_PREFIX}{ident}"


def _admin_login_identifier(request) -> tuple[str, bool]:
    """Return (identifier, use_email) from POST body (email preferred when present)."""
    email = (request.data.get("email") or "").strip()
    phone = (request.data.get("phone") or "").strip()
    if email and "@" in email:
        return email, True
    if phone and "@" in phone:
        return phone, True
    return phone or email, False


@api_view(["POST"])
@throttle_classes([AdminLoginThrottle])
@permission_classes([AllowAny])
def admin_login(request):
    identifier, use_email = _admin_login_identifier(request)
    password = request.data.get("password", "")
    fail_key = _admin_login_fail_cache_key(identifier, use_email=use_email)
    if use_email:
        user = authenticate_user_by_email(request, identifier, password)
        find_guessed = lambda: find_user_by_email(identifier)
    else:
        user = authenticate_user_by_phone(request, identifier, password)
        find_guessed = lambda: find_user_by_phone_input(identifier)
    ip = client_ip_from_request(request)
    ss = SecuritySettings.load()

    if user:
        cache.delete(fail_key)
    elif (
        ss.auto_lock_failed_logins
        and (guessed_user := find_guessed())
        and guessed_user.is_active
        and not getattr(guessed_user, "is_superuser", False)
    ):
        n = int(cache.get(fail_key) or 0) + 1
        cache.set(fail_key, n, ADMIN_LOGIN_FAIL_TTL)
        if n >= ADMIN_LOGIN_FAIL_MAX:
            guessed_user.is_active = False
            guessed_user.save(update_fields=["is_active"])
            security_service.flag_and_log_security_event(
                activity_type="Admin account auto-locked",
                detail="Repeated failed admin logins; account deactivated.",
                severity="high",
                user=guessed_user,
                ip_address=ip,
                performed_by=guessed_user,
                action_kind=AuditLog.ActionKind.UPDATE,
                module="auth",
                metadata={"portal": "admin", "failures": n},
            )

    if not user:
        guessed_user = find_guessed()
        security_service.flag_and_log_security_event(
            activity_type="Admin login failed",
            detail="Invalid admin credentials supplied.",
            severity="medium",
            user=guessed_user,
            ip_address=ip,
            performed_by=guessed_user,
            action_kind=AuditLog.ActionKind.LOGIN,
            module="auth",
            metadata={
                "portal": "admin",
                "login_method": "email" if use_email else "phone",
                "identifier": identifier[:30],
            },
        )
        return Response({"detail": "Invalid credentials."}, status=400)
    denied = assert_portal_login_allowed(user, PORTAL_ADMIN)
    if denied:
        security_service.flag_and_log_security_event(
            activity_type="Admin login denied",
            detail="User authenticated but is not allowed to access admin portal.",
            severity="high",
            user=user,
            ip_address=ip,
            performed_by=user,
            action_kind=AuditLog.ActionKind.LOGIN,
            module="auth",
            metadata={"portal": "admin", "reason": "wrong_portal_role"},
        )
        return denied

    token, _ = Token.objects.get_or_create(user=user)
    audit_service.log(
        "Admin login",
        log_type=AuditLog.Type.SECURITY,
        performed_by=user,
        action_kind=AuditLog.ActionKind.LOGIN,
        module="auth",
        ip_address=ip,
        metadata={"portal": "admin"},
    )
    u = _annotate_admin_user_customer_metrics(User.objects.filter(pk=user.pk)).first()
    return Response(
        {
            "token": token.key,
            "user": AdminUserSerializer(u, context={"request": request}).data,
        }
    )


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_logout(request):
    if request.auth:
        request.auth.delete()
    return Response({"detail": "Logged out."})


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_change_password(request):
    if not user_allowed_for_admin_portal(request.user):
        return Response({"detail": "Admin access required."}, status=403)
    old_p = request.data.get("old_password") or ""
    new_p = request.data.get("new_password") or ""
    if not new_p or len(new_p) < 6:
        return validation_error("new_password must be at least 6 characters", field="new_password")
    u = request.user
    if not u.check_password(old_p):
        security_service.flag_and_log_security_event(
            activity_type="Admin password change failed",
            detail="Current password mismatch during change-password attempt.",
            severity="medium",
            user=u,
            ip_address=client_ip_from_request(request),
            performed_by=u,
            action_kind=AuditLog.ActionKind.UPDATE,
            module="auth",
            metadata={"portal": "admin"},
        )
        return Response({"detail": "Current password is incorrect."}, status=400)
    u.set_password(new_p)
    u.save(update_fields=["password"])
    audit_service.log(
        "Admin password changed",
        log_type=AuditLog.Type.SECURITY,
        performed_by=u,
        action_kind=AuditLog.ActionKind.UPDATE,
        module="auth",
        ip_address=client_ip_from_request(request),
        metadata={"portal": "admin"},
    )
    return Response({"ok": True})


def _admin_self_profile_payload(request, u: User) -> dict:
    ep = (
        EmployeeProfile.objects.filter(user=u)
        .select_related("role")
        .first()
    )
    employee = None
    if ep:
        employee = {
            "status": ep.status,
            "role_name": ep.role.name if ep.role_id else "",
            "modules_access": ep.modules_access if isinstance(ep.modules_access, list) else [],
        }
    return {
        "id": u.pk,
        "name": u.name,
        "phone": u.phone,
        "email": u.email or "",
        "address": u.address or "",
        "role": u.role,
        "avatar_url": absolute_media_url(request, u.avatar),
        "is_superuser": u.is_superuser,
        "employee": employee,
        "groups": rbac.user_groups_payload(u),
        "permissions": rbac.user_permission_strings(u),
    }


@api_view(["GET", "PATCH"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_self_profile(request):
    if not user_allowed_for_admin_portal(request.user):
        return Response({"detail": "Admin access required."}, status=403)
    u = request.user
    if request.method == "GET":
        return Response(_admin_self_profile_payload(request, u))
    if "name" in request.data:
        u.name = (request.data.get("name") or "").strip()[:150] or u.name
    if "email" in request.data:
        u.email = (request.data.get("email") or "").strip()[:254]
    if "phone" in request.data:
        phone = (request.data.get("phone") or "").strip()[:15]
        if phone and phone != u.phone:
            if User.objects.filter(phone=phone).exclude(pk=u.pk).exists():
                return validation_error("Phone already in use.", field="phone")
            u.phone = phone
            u.username = phone
    if "address" in request.data:
        u.address = (request.data.get("address") or "")[:2000]
    if request.FILES.get("avatar"):
        u.avatar = request.FILES["avatar"]
    u.save()
    u.refresh_from_db()
    return Response(_admin_self_profile_payload(request, u))


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def users_list(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    queryset = User.objects.all().order_by("-created_at")

    customers_only = request.query_params.get("customers_only")
    if customers_only in ("true", "1", "True"):
        queryset = queryset.filter(
            role__in=[
                User.Role.NORMAL,
                User.Role.PARENT,
                User.Role.CHILD,
            ]
        ).exclude(pk__in=Vendor.objects.values_list("user_id", flat=True))
        queryset = queryset.exclude(is_staff=True)

    role = request.query_params.get("role")
    kyc_status = request.query_params.get("kyc_status")
    search = request.query_params.get("search")

    if role:
        queryset = queryset.filter(role=role)
    if kyc_status:
        queryset = queryset.filter(kyc_status=kyc_status)
    if search:
        q = (
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(email__icontains=search)
            | Q(username__icontains=search)
        )
        if search.isdigit():
            try:
                q |= Q(pk=int(search))
            except (ValueError, OverflowError):
                pass
        queryset = queryset.filter(q)

    queryset = _annotate_admin_user_customer_metrics(queryset)

    paginator = UserPagination()
    page = paginator.paginate_queryset(queryset, request)
    serializer = AdminUserSerializer(
        page, many=True, context={"request": request}
    )
    return paginator.get_paginated_response(serializer.data)


@api_view(["POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_user_create(request):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    phone = (request.data.get("phone") or "").strip()
    name = (request.data.get("name") or "").strip()
    if not phone or not name:
        return validation_error("phone and name are required")
    if User.objects.filter(phone=phone).exists():
        return validation_error("phone already registered")
    role = request.data.get("role") or User.Role.NORMAL
    if role not in dict(User.Role.choices):
        return validation_error("invalid role")
    is_staff = bool(request.data.get("is_staff"))
    if is_staff and not request.user.is_superuser:
        return Response({"detail": "Only superuser can create staff accounts."}, status=403)
    user = User.objects.create_user(
        username=phone,
        email=(request.data.get("email") or "").strip(),
        password=request.data.get("password") or None,
        name=name,
        phone=phone,
        role=role,
        is_staff=is_staff,
        kyc_status=request.data.get("kyc_status") or User.KYCStatus.PENDING,
    )
    if not request.data.get("password"):
        user.set_unusable_password()
        user.save(update_fields=["password"])
    file_fields = []
    if request.FILES.get("avatar"):
        user.avatar = request.FILES["avatar"]
        file_fields.append("avatar")
    if request.FILES.get("customer_document"):
        err = _validate_customer_document_file(request.FILES["customer_document"])
        if err:
            return err
        user.customer_document = request.FILES["customer_document"]
        file_fields.append("customer_document")
    if file_fields:
        user.save(update_fields=file_fields)
    if "group_ids" in request.data:
        rbac.assign_user_groups(user, request.data.get("group_ids"))
    u = _annotate_admin_user_customer_metrics(User.objects.filter(pk=user.pk)).first()
    return Response(
        AdminUserSerializer(u, context={"request": request}).data, status=201
    )


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_user_detail_write(request, pk):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    user = _annotate_admin_user_customer_metrics(User.objects.filter(pk=pk)).first()
    if not user:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        data = AdminUserSerializer(user, context={"request": request}).data
        data["avatar"] = absolute_media_url(request, user.avatar) if user.avatar else ""
        return Response(data)
    if request.method == "DELETE":
        delete_err = _admin_user_delete_forbidden(request, user)
        if delete_err:
            return Response({"detail": delete_err}, status=403)
        deleted_name = user.name
        deleted_phone = user.phone
        deleted_id = user.pk
        user.delete()
        audit_service.log(
            f"Admin deleted user {deleted_name!r} (id={deleted_id})",
            log_type=AuditLog.Type.USER,
            performed_by=request.user,
            object_type="User",
            object_id=str(deleted_id),
            ip_address=client_ip_from_request(request),
            action_kind=AuditLog.ActionKind.DELETE,
            module="users",
            metadata={"phone": deleted_phone},
        )
        return Response({"ok": True})
    if "name" in request.data:
        user.name = (request.data.get("name") or "").strip() or user.name
    if "email" in request.data:
        user.email = (request.data.get("email") or "").strip()
    if "phone" in request.data:
        new_phone = (request.data.get("phone") or "").strip()
        if new_phone and new_phone != user.phone:
            if User.objects.filter(phone=new_phone).exclude(pk=user.pk).exists():
                return validation_error("phone already in use")
            user.phone = new_phone
            user.username = new_phone
    if "role" in request.data:
        user.role = request.data.get("role")
    if "kyc_status" in request.data:
        user.kyc_status = request.data.get("kyc_status")
    if "is_active" in request.data:
        user.is_active = request.data.get("is_active") in (True, "true", "1", 1)
    if "is_staff" in request.data and request.user.is_superuser:
        user.is_staff = request.data.get("is_staff") in (True, "true", "1", 1)
    if request.FILES.get("avatar"):
        user.avatar = request.FILES["avatar"]
    if request.FILES.get("customer_document"):
        err = _validate_customer_document_file(request.FILES["customer_document"])
        if err:
            return err
        user.customer_document = request.FILES["customer_document"]
    if "group_ids" in request.data:
        rbac.assign_user_groups(user, request.data.get("group_ids"))
    pwd = request.data.get("password")
    if pwd is not None and str(pwd).strip():
        if not request.user.is_superuser:
            return Response(
                {"detail": "Only superuser can reset passwords for other accounts."},
                status=403,
            )
        user.set_password(str(pwd).strip())
    user.save()
    audit_service.log(
        f"Admin updated user {user.name!r} (id={user.pk})",
        log_type=AuditLog.Type.USER,
        performed_by=request.user,
        object_type="User",
        object_id=str(user.pk),
        ip_address=client_ip_from_request(request),
        action_kind=AuditLog.ActionKind.UPDATE,
        module="users",
        metadata={"phone": user.phone, "role": user.role},
    )
    u = _annotate_admin_user_customer_metrics(User.objects.filter(pk=user.pk)).first()
    data = AdminUserSerializer(u, context={"request": request}).data
    data["avatar"] = absolute_media_url(request, user.avatar) if user.avatar else ""
    return Response(data)


_PARENT_MEMBER_ROLES = frozenset(
    {
        FamilyMember.Role.PARENT,
        FamilyMember.Role.MANAGER,
        FamilyMember.Role.SPOUSE,
    }
)


def _admin_customer_user_row(request, user, *, membership_role=None):
    data = AdminUserSerializer(user, context={"request": request}).data
    if membership_role:
        data["membership_role"] = membership_role
    return data


@api_view(["GET"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_customers_grouped(request):
    """Family groups with parent accounts and nested child accounts for admin customers page."""
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden

    groups_qs = (
        FamilyGroup.objects.filter(is_platform_hub=False)
        .select_related("leader")
        .prefetch_related(
            Prefetch(
                "members",
                queryset=FamilyMember.objects.select_related("user").order_by("joined_at"),
            )
        )
        .order_by("name")
    )

    user_ids: set[int] = set()
    group_snapshots = []
    for group in groups_qs:
        members = list(group.members.all())
        if not members:
            continue
        parent_members = [m for m in members if m.role in _PARENT_MEMBER_ROLES]
        child_members = [m for m in members if m.role == FamilyMember.Role.CHILD]
        if not parent_members and not child_members:
            continue
        for m in members:
            user_ids.add(m.user_id)
        user_ids.add(group.leader_id)
        group_snapshots.append((group, parent_members, child_members))

    users_by_id = {
        u.pk: u
        for u in _annotate_admin_user_customer_metrics(
            User.objects.filter(pk__in=user_ids)
        )
    }

    groups_out = []
    for group, parent_members, child_members in group_snapshots:
        children_payload = []
        for m in child_members:
            u = users_by_id.get(m.user_id)
            if u:
                children_payload.append(
                    _admin_customer_user_row(request, u, membership_role=m.role)
                )

        primary_uid = group.leader_id
        if parent_members and not any(m.user_id == primary_uid for m in parent_members):
            primary_uid = parent_members[0].user_id

        parents_payload = []
        seen_parent_uids: set[int] = set()
        for m in parent_members:
            u = users_by_id.get(m.user_id)
            if not u:
                continue
            node = _admin_customer_user_row(request, u, membership_role=m.role)
            node["children"] = children_payload if m.user_id == primary_uid else []
            parents_payload.append(node)
            seen_parent_uids.add(m.user_id)

        if child_members and group.leader_id and group.leader_id not in seen_parent_uids:
            leader = users_by_id.get(group.leader_id)
            if leader:
                node = _admin_customer_user_row(
                    request, leader, membership_role="leader"
                )
                node["children"] = children_payload
                parents_payload.insert(0, node)
                seen_parent_uids.add(group.leader_id)

        if not parents_payload and child_members:
            leader = users_by_id.get(group.leader_id)
            if leader:
                node = _admin_customer_user_row(
                    request, leader, membership_role="leader"
                )
                node["children"] = children_payload
                parents_payload.append(node)

        groups_out.append(
            {
                "id": str(group.pk),
                "name": group.name,
                "type": group.type,
                "status": group.status,
                "parents": parents_payload,
            }
        )

    return Response({"groups": groups_out})


@api_view(["GET", "POST"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_user_kyc_documents(request, pk):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    user = User.objects.filter(pk=pk).first()
    if not user:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        rows = []
        for d in user.kyc_documents.all():
            rows.append(
                {
                    "id": str(d.pk),
                    "document_type": d.document_type,
                    "status": d.status,
                    "document_image": absolute_media_url(request, d.document_image)
                    if d.document_image
                    else "",
                    "document_back": absolute_media_url(request, d.document_back)
                    if d.document_back
                    else "",
                    "document_file": absolute_media_url(request, d.document_file) if d.document_file else "",
                    "document_id_number": (d.document_id_number or "").strip(),
                    "submitted_at": d.submitted_at.isoformat(),
                }
            )
        return Response({"documents": rows})
    doc_type = (request.data.get("document_type") or "").strip()
    if doc_type not in {c[0] for c in KYCDocument.DocumentType.choices}:
        return validation_error("document_type is required and must be valid")
    img = request.FILES.get("document_image") or request.FILES.get("image")
    pdf = request.FILES.get("document_file") or request.FILES.get("file")
    back = request.FILES.get("document_back")
    for f, name in ((img, "document_image"), (back, "document_back"), (pdf, "document_file")):
        if f:
            err = validate_kyc_upload_file(f, name)
            if err:
                return err
    if not img and not pdf:
        return validation_error("upload document_image or document_file")
    id_num = (request.data.get("document_id_number") or "").strip()[:100]
    supersede_non_approved_kyc(user, doc_type)
    row = KYCDocument(
        user=user,
        document_type=doc_type,
        status=KYCDocument.Status.PENDING,
        document_id_number=id_num,
    )
    if img:
        row.document_image = img
    if pdf:
        row.document_file = pdf
    if back:
        row.document_back = back
    row.save()
    sync_user_kyc_status(user)
    return Response({"id": str(row.pk)}, status=201)

