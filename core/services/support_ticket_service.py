"""Support ticket threads and portal source classification."""

from __future__ import annotations

import mimetypes
import os
from collections.abc import Callable, Sequence

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse
from django.utils import timezone

from core.models import (
    SupportTicket,
    SupportTicketMessage,
    SupportTicketMessageAttachment,
    SupportTicketReaderState,
    User,
)
from core.portal_roles import user_has_family_portal_access

MAX_SUPPORT_MESSAGE_CHARS = 8000

_ALLOWED_MIME_PREFIX = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
)

_ALLOWED_EXT = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".mp4",
        ".webm",
        ".pdf",
        ".doc",
        ".docx",
    }
)


def _max_attachment_bytes() -> int:
    return int(getattr(settings, "SUPPORT_CHAT_MAX_ATTACHMENT_BYTES", 25 * 1024 * 1024))


def _max_attachments_per_message() -> int:
    return int(getattr(settings, "SUPPORT_CHAT_MAX_ATTACHMENTS_PER_MESSAGE", 5))


def sender_role_kind(user: User) -> str:
    if user.is_authenticated and user.is_staff:
        return "staff"
    return "user"


def resolve_portal_source_panel(user: User) -> str:
    if user.role == User.Role.CHILD:
        return SupportTicket.SourcePanel.CHILD
    if user_has_family_portal_access(user):
        return SupportTicket.SourcePanel.FAMILY
    return SupportTicket.SourcePanel.CUSTOMER


def attachment_kind(content_type: str, filename: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    return "document"


def _normalize_upload_mime(name: str, declared: str | None) -> str:
    ext = os.path.splitext(name or "")[1].lower()
    if ext in _ALLOWED_EXT:
        guessed, _ = mimetypes.guess_type(name or "")
        if guessed:
            return guessed.split(";")[0].strip().lower()
    d = (declared or "").split(";")[0].strip().lower()
    return d


def validate_uploaded_file(f) -> tuple[str, str]:
    """
    Validate a Django UploadedFile. Returns (effective_mime, original_name).
    Raises ValueError on failure.
    """
    name = getattr(f, "name", "") or "file"
    if len(name) > 255:
        raise ValueError("file name too long")
    ext = os.path.splitext(name)[1].lower()
    if ext not in _ALLOWED_EXT:
        raise ValueError("file type not allowed")
    size = int(getattr(f, "size", 0) or 0)
    if size <= 0:
        raise ValueError("empty file")
    if size > _max_attachment_bytes():
        raise ValueError("file too large")
    mime = _normalize_upload_mime(name, getattr(f, "content_type", None))
    if mime not in _ALLOWED_MIME_PREFIX:
        raise ValueError("file type not allowed")
    return mime, name


def message_attachment_row(
    att: SupportTicketMessageAttachment,
    url_fn: Callable[[int], str],
) -> dict:
    ct = att.content_type or "application/octet-stream"
    return {
        "id": str(att.pk),
        "filename": att.original_name,
        "kind": attachment_kind(ct, att.original_name),
        "mime_type": ct,
        "url": url_fn(att.pk),
    }


def message_to_row(
    m: SupportTicketMessage,
    attachment_url_fn: Callable[[int], str] | None = None,
    *,
    sender_avatar_url_fn: Callable[[User], str] | None = None,
    delivery_ticks: int | None = None,
) -> dict:
    u = m.sender
    atts = []
    if attachment_url_fn is not None:
        for att in m.attachments.all():
            atts.append(message_attachment_row(att, attachment_url_fn))
    avatar_url = ""
    if sender_avatar_url_fn is not None:
        avatar_url = sender_avatar_url_fn(u) or ""
    row = {
        "id": str(m.pk),
        "sender_id": u.pk,
        "sender_name": u.name or u.phone or str(u.pk),
        "sender_role_kind": sender_role_kind(u),
        "sender_avatar_url": avatar_url,
        "body": m.body,
        "created_at": m.created_at.isoformat(),
        "attachments": atts,
    }
    if delivery_ticks is not None:
        row["delivery_ticks"] = delivery_ticks
    return row


def primary_super_admin_user() -> User | None:
    return (
        User.objects.filter(Q(role=User.Role.SUPER_ADMIN) | Q(is_superuser=True))
        .order_by("id")
        .first()
    )


def super_admin_user_ids() -> list[int]:
    return list(
        User.objects.filter(Q(role=User.Role.SUPER_ADMIN) | Q(is_superuser=True))
        .values_list("pk", flat=True)[:50]
    )


def mark_ticket_read(ticket: SupportTicket, reader: User) -> None:
    now = timezone.now()
    SupportTicketReaderState.objects.update_or_create(
        ticket=ticket,
        reader=reader,
        defaults={"last_read_at": now},
    )


def ticket_has_unread_for_submitter(
    ticket: SupportTicket,
    *,
    state: SupportTicketReaderState | None,
) -> bool:
    lr = state.last_read_at if state else None
    q = SupportTicketMessage.objects.filter(ticket=ticket, sender__is_staff=True)
    if lr:
        q = q.filter(created_at__gt=lr)
    return q.exists()


def ticket_has_unread_for_staff_reader(
    ticket: SupportTicket,
    *,
    state: SupportTicketReaderState | None,
) -> bool:
    lr = state.last_read_at if state else None
    q = SupportTicketMessage.objects.filter(ticket=ticket, sender__is_staff=False)
    if lr:
        q = q.filter(created_at__gt=lr)
    return q.exists()


def last_message_preview_text(ticket: SupportTicket) -> str:
    last = (
        SupportTicketMessage.objects.filter(ticket=ticket)
        .prefetch_related("attachments")
        .order_by("-created_at", "-id")
        .first()
    )
    if not last:
        return ""
    body = (last.body or "").strip()
    if len(body) > 80:
        return body[:77] + "…"
    if body:
        return body
    att = last.attachments.first()
    if att:
        return f"📎 {(att.original_name or 'file')[:60]}"
    return "…"


def delivery_tick_for_message(
    m: SupportTicketMessage,
    *,
    viewer_user_id: int,
    viewer_is_staff: bool,
    counterpart_online: bool,
) -> int | None:
    tick = 2 if counterpart_online else 1
    if viewer_is_staff:
        if sender_role_kind(m.sender) != "staff":
            return None
    else:
        if m.sender_id != viewer_user_id:
            return None
    return tick


def serialize_ticket_messages(
    messages: Sequence[SupportTicketMessage],
    attachment_url_fn: Callable[[int], str],
    *,
    sender_avatar_url_fn: Callable[[User], str] | None,
    viewer_user_id: int,
    viewer_is_staff: bool,
    counterpart_online: bool,
) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        tick = delivery_tick_for_message(
            m,
            viewer_user_id=viewer_user_id,
            viewer_is_staff=viewer_is_staff,
            counterpart_online=counterpart_online,
        )
        out.append(
            message_to_row(
                m,
                attachment_url_fn,
                sender_avatar_url_fn=sender_avatar_url_fn,
                delivery_ticks=tick,
            )
        )
    return out


def append_message(
    ticket: SupportTicket,
    sender: User,
    body: str,
    uploaded_files: Sequence | None = None,
) -> SupportTicketMessage:
    text = (body or "").strip()
    files = list(uploaded_files or [])
    if _max_attachments_per_message() and len(files) > _max_attachments_per_message():
        raise ValueError(f"at most {_max_attachments_per_message()} files per message")
    if not text and not files:
        raise ValueError("body or at least one file required")
    if text and len(text) > MAX_SUPPORT_MESSAGE_CHARS:
        raise ValueError(f"body must be at most {MAX_SUPPORT_MESSAGE_CHARS} characters")

    validated: list[tuple[object, str, str]] = []
    for f in files:
        mime, orig_name = validate_uploaded_file(f)
        validated.append((f, mime, orig_name))

    now = timezone.now()
    with transaction.atomic():
        msg = SupportTicketMessage.objects.create(
            ticket=ticket,
            sender=sender,
            body=text,
        )
        for f, mime, orig_name in validated:
            size = int(getattr(f, "size", 0) or 0)
            att = SupportTicketMessageAttachment(
                message=msg,
                original_name=orig_name[:255],
                size=size,
                content_type=mime,
            )
            att.file.save(orig_name, f, save=False)
            att.save()
        ticket.last_activity_at = now
        ticket.save(update_fields=["last_activity_at"])
    return msg


def messages_page_before(
    ticket: SupportTicket,
    before_id: int,
    limit: int,
    attachment_url_fn: Callable[[int], str],
    *,
    sender_avatar_url_fn: Callable[[User], str] | None = None,
    viewer_user_id: int | None = None,
    viewer_is_staff: bool = False,
    counterpart_online: bool = False,
) -> tuple[list[dict], bool]:
    """Older messages with pk < before_id, chronological within the page."""
    lim = max(1, min(limit, 100))
    qs = (
        ticket.messages.filter(pk__lt=before_id)
        .select_related("sender")
        .prefetch_related("attachments")
        .order_by("-pk")[: lim + 1]
    )
    raw = list(qs)
    has_more = len(raw) > lim
    raw = raw[:lim]
    raw.reverse()
    tick_meta = viewer_user_id is not None
    out: list[dict] = []
    for m in raw:
        tick = None
        if tick_meta:
            tick = delivery_tick_for_message(
                m,
                viewer_user_id=viewer_user_id,
                viewer_is_staff=viewer_is_staff,
                counterpart_online=counterpart_online,
            )
        out.append(
            message_to_row(
                m,
                attachment_url_fn,
                sender_avatar_url_fn=sender_avatar_url_fn,
                delivery_ticks=tick,
            )
        )
    return out, has_more


def ensure_initial_message(ticket: SupportTicket) -> None:
    """Create first chat row from description if missing (e.g. legacy data)."""
    if ticket.messages.exists():
        return
    SupportTicketMessage.objects.create(
        ticket=ticket,
        sender=ticket.submitter,
        body=ticket.description or "",
    )
    if ticket.last_activity_at is None:
        ticket.last_activity_at = ticket.created_at
        ticket.save(update_fields=["last_activity_at"])


def get_attachment_or_none(attachment_id: int) -> SupportTicketMessageAttachment | None:
    return (
        SupportTicketMessageAttachment.objects.filter(pk=attachment_id)
        .select_related("message__ticket")
        .first()
    )


def user_may_access_ticket(user: User, ticket: SupportTicket) -> bool:
    return user.is_authenticated and ticket.submitter_id == user.pk


def user_may_access_attachment_for_submitter(user: User, att: SupportTicketMessageAttachment) -> bool:
    return user_may_access_ticket(user, att.message.ticket)


def extract_message_body_and_files_from_request(request) -> tuple[str, list]:
    """DRF request: JSON body or multipart with optional files (files or file)."""
    ctype = (getattr(request, "content_type", None) or "").lower()
    if "multipart/form-data" in ctype:
        body = request.POST.get("body", "") or ""
        fl = list(request.FILES.getlist("files"))
        if not fl:
            fl = list(request.FILES.getlist("file"))
        return str(body).strip(), fl
    raw = request.data.get("body") if hasattr(request.data, "get") else None
    if raw is None:
        raw = ""
    return str(raw).strip(), []


def attachment_file_response(att: SupportTicketMessageAttachment) -> FileResponse:
    """Inline preview for image/video/pdf; attachment download for other documents."""
    ct = (att.content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    k = attachment_kind(ct, att.original_name)
    inline = k in ("image", "video") or ct == "application/pdf"
    att.file.open("rb")
    return FileResponse(
        att.file,
        as_attachment=not inline,
        filename=att.original_name or "file",
        content_type=ct or "application/octet-stream",
    )
