import os

from django.conf import settings
from core.phone_auth import authenticate_user_by_phone
from django.db.models import Q
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from core.models import AuditLog, EmployeeProfile, KYCDocument, User, Vendor
from core.portal_roles import PORTAL_ADMIN, assert_portal_login_allowed, user_allowed_for_admin_portal
from core.services import audit_service
from core.services.kyc_portal import supersede_non_approved_kyc, validate_kyc_upload_file
from core.services.kyc_service import sync_user_kyc_status
from core.serializers import AdminUserSerializer
from core.views.admin.admin_write_utils import absolute_media_url, client_ip_from_request, validation_error


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


class UserPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


@api_view(["POST"])
@permission_classes([AllowAny])
def admin_login(request):
    phone = request.data.get("phone", "").strip()
    password = request.data.get("password", "")
    user = authenticate_user_by_phone(request, phone, password)

    if not user:
        return Response({"detail": "Invalid credentials."}, status=400)
    denied = assert_portal_login_allowed(user, PORTAL_ADMIN)
    if denied:
        return denied

    token, _ = Token.objects.get_or_create(user=user)
    audit_service.log(
        "Admin login",
        log_type=AuditLog.Type.SECURITY,
        performed_by=user,
        action_kind=AuditLog.ActionKind.LOGIN,
        module="auth",
        ip_address=client_ip_from_request(request),
        metadata={"portal": "admin"},
    )
    return Response(
        {
            "token": token.key,
            "user": AdminUserSerializer(user).data,
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
        return Response({"detail": "Current password is incorrect."}, status=400)
    u.set_password(new_p)
    u.save(update_fields=["password"])
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
    return Response(
        AdminUserSerializer(user, context={"request": request}).data, status=201
    )


@api_view(["GET", "PATCH", "DELETE"])
@authentication_classes([TokenAuthentication, SessionAuthentication])
@permission_classes([IsAuthenticated])
def admin_user_detail_write(request, pk):
    forbidden = _forbidden_if_not_admin(request)
    if forbidden:
        return forbidden
    user = User.objects.filter(pk=pk).first()
    if not user:
        return Response({"detail": "Not found."}, status=404)
    if request.method == "GET":
        data = AdminUserSerializer(user, context={"request": request}).data
        data["avatar"] = absolute_media_url(request, user.avatar) if user.avatar else ""
        return Response(data)
    if request.method == "DELETE":
        user.delete()
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
    data = AdminUserSerializer(user, context={"request": request}).data
    data["avatar"] = absolute_media_url(request, user.avatar) if user.avatar else ""
    return Response(data)


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

