"""Session-based online detection (same heuristic as family portal member lists)."""

from __future__ import annotations

from django.contrib.sessions.models import Session
from django.utils import timezone


def online_user_ids_for(user_ids: list[int]) -> set[int]:
    """Treat users with at least one non-expired Django session as online."""
    if not user_ids:
        return set()
    want = {int(uid) for uid in user_ids}
    online: set[int] = set()
    active_sessions = Session.objects.filter(expire_date__gte=timezone.now())
    for session in active_sessions.iterator():
        data = session.get_decoded()
        raw_uid = data.get("_auth_user_id")
        if raw_uid is None:
            continue
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        if uid in want:
            online.add(uid)
    return online
