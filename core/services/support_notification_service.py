"""In-app notifications for support ticket threads."""

from __future__ import annotations

from urllib.parse import quote

from core.models import Notification, SupportTicket, User
from core.portal_roles import user_allowed_for_admin_portal


def _ticket_query(ticket_number: str) -> str:
    return f"?ticket={quote(str(ticket_number), safe='')}"


def _action_url_for_submitter(ticket: SupportTicket) -> str:
    q = _ticket_query(ticket.ticket_number)
    sp = ticket.source_panel
    if sp == SupportTicket.SourcePanel.VENDOR:
        return f"/vendor/tickets{q}"
    if sp == SupportTicket.SourcePanel.FAMILY:
        return f"/family-portal/support{q}"
    if sp == SupportTicket.SourcePanel.CHILD:
        return f"/child-portal/help{q}"
    return f"/portal/support{q}"


def notify_submitter_staff_replied(ticket: SupportTicket) -> None:
    """Customer/vendor/parent/child sees a notification when staff replies."""
    u = ticket.submitter
    if ticket.source_panel == SupportTicket.SourcePanel.VENDOR:
        target = Notification.Target.VENDORS
    else:
        target = Notification.Target.CUSTOMERS
    Notification.objects.create(
        title="Support reply",
        message=f"New reply on ticket {ticket.ticket_number}: {ticket.subject[:80]}",
        type=Notification.Type.SUPPORT,
        target=target,
        recipient=u,
        action_url=_action_url_for_submitter(ticket)[:255],
    )


def notify_admins_ticket_activity(ticket: SupportTicket, preview: str) -> None:
    """Each eligible admin user gets their own notification row (read state per user)."""
    preview = (preview or "").strip()[:200]
    base_url = f"/admin/support-tickets{_ticket_query(ticket.ticket_number)}"
    for admin_user in User.objects.filter(is_active=True).iterator():
        if not user_allowed_for_admin_portal(admin_user):
            continue
        Notification.objects.create(
            title="Support ticket activity",
            message=f"{ticket.ticket_number}: {preview}",
            type=Notification.Type.SUPPORT,
            target=Notification.Target.ADMINS,
            recipient=admin_user,
            action_url=base_url[:255],
        )


def notify_admins_new_ticket(ticket: SupportTicket) -> None:
    notify_admins_ticket_activity(
        ticket,
        f"New ticket — {ticket.subject}",
    )
