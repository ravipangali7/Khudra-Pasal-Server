"""Google / Facebook OAuth: authorization code flow, DRF token + redirect to SPA."""

from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response

from core.models import User
from core.portal_roles import (
    PORTAL_FAMILY,
    PORTAL_MAIN,
    assert_portal_login_allowed,
    infer_portal_key_from_frontend_path,
    primary_spa_redirect,
)
from core.services import family_service
from core.services.base import get_or_create_personal_wallet
from core.views.unified_auth import build_auth_response_for_portal

logger = logging.getLogger(__name__)

# Shown when GOOGLE_OAUTH_CLIENT_ID / SECRET are missing from server/.env (or credentials JSON).
_GOOGLE_OAUTH_NOT_CONFIGURED = (
    "Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET "
    "in server/.env (see server/.env.example), then restart Django."
)

OAUTH_STATE_SALT = "khudrapasal-oauth-state"
OAUTH_PENDING_SALT = "khudrapasal-oauth-pending-phone"


def _frontend_base() -> str:
    return (getattr(settings, "FRONTEND_URL", None) or "http://localhost:5173").rstrip("/")


def _public_api_base(request) -> str:
    base = getattr(settings, "OAUTH_REDIRECT_BASE", None)
    if base:
        return base.rstrip("/")
    return request.build_absolute_uri("/")[:-1]


def _normalize_error_return_path(raw: str | None, default: str = "/login") -> str:
    p = (raw or default or "/login").strip() or default
    if not p.startswith("/") or p.startswith("//"):
        return default
    return p[:200]


def _sign_state(
    provider: str,
    next_path: str,
    error_return: str = "/login",
    flow: str = "login",
) -> str:
    er = _normalize_error_return_path(error_return, "/login")
    fw = flow if flow in ("login", "register") else "login"
    return signing.dumps(
        {
            "p": provider,
            "n": (next_path or "")[:500],
            "e": er,
            "f": fw,
        },
        salt=OAUTH_STATE_SALT,
    )


def _read_state(state: str) -> tuple[str, str, str, str]:
    data = signing.loads(state, max_age=900, salt=OAUTH_STATE_SALT)
    er = _normalize_error_return_path(str(data.get("e") or ""), "/login")
    fw = str(data.get("f") or "login")
    if fw not in ("login", "register"):
        fw = "login"
    return str(data.get("p", "")), str(data.get("n", "")), er, fw


def _effective_portal_key_for_oauth(user: User, next_path: str) -> str:
    """
    Prefer the portal implied by `next`, but if the user opened Google from the generic
    customer login/signup (main portal) and their role does not match, use their primary SPA home.
    """
    portal_key = infer_portal_key_from_frontend_path(next_path)
    if assert_portal_login_allowed(user, portal_key) is None:
        return portal_key
    if portal_key != PORTAL_MAIN:
        return portal_key
    alt_path = primary_spa_redirect(user)
    alt_key = infer_portal_key_from_frontend_path(alt_path)
    if assert_portal_login_allowed(user, alt_key) is None:
        return alt_key
    return portal_key


def _allocate_placeholder_phone() -> str:
    for _ in range(100):
        digits = "98" + "".join(str(secrets.randbelow(10)) for _ in range(8))
        if not User.objects.filter(phone=digits).exists():
            return digits
    raise RuntimeError("Could not allocate unique placeholder phone")


def _redirect_to_frontend(query: dict, return_path: str = "/login") -> HttpResponseRedirect:
    """Merge `query` into the SPA return path (supports `return_path` that already includes `?`)."""
    rp = _normalize_error_return_path(return_path, "/login")
    base = _frontend_base()
    merged: dict[str, str] = {}
    if "?" in rp:
        path_only, _, qs = rp.partition("?")
        merged.update(dict(urllib.parse.parse_qsl(qs, keep_blank_values=True)))
    else:
        path_only = rp
    for k, v in query.items():
        if v is not None and v != "":
            merged[k] = str(v)
    q = urllib.parse.urlencode(merged)
    url = f"{base}{path_only}"
    if q:
        url = f"{url}?{q}"
    return HttpResponseRedirect(url)


def sign_oauth_pending_token(user_id: int, next_path: str) -> str:
    return signing.dumps(
        {"uid": int(user_id), "n": (next_path or "")[:500]},
        salt=OAUTH_PENDING_SALT,
    )


def read_oauth_pending_token(token: str, max_age: int = 1800) -> tuple[int, str]:
    data = signing.loads(token, max_age=max_age, salt=OAUTH_PENDING_SALT)
    return int(data["uid"]), str(data.get("n") or "")


def _normalize_oauth_avatar_url(raw: str | None) -> str:
    """Google `picture` is an HTTPS URL; cap length for URLField(512)."""
    s = (raw or "").strip()
    if not s.startswith("http"):
        return ""
    return s[:512]


def _get_or_create_social_user(
    provider: str,
    provider_user_id: str,
    name: str,
    email: str,
    *,
    avatar_url: str = "",
) -> tuple[User, bool]:
    """
    Returns (user, created_new_account).
    created_new_account is True only when a brand-new User row was inserted (not social/email link).
    Persists provider profile image on ``User.social_avatar_url`` when supplied.
    """
    sp = User.SocialProvider.GOOGLE if provider == "google" else User.SocialProvider.FACEBOOK
    pic = _normalize_oauth_avatar_url(avatar_url)

    existing = User.objects.filter(social_provider=sp, social_provider_id=provider_user_id).first()
    if existing:
        # Refresh display name / picture from the provider on each successful OAuth login.
        update_fields: list[str] = []
        if name and existing.name != name[:150]:
            existing.name = name[:150]
            update_fields.append("name")
        if pic and existing.social_avatar_url != pic:
            existing.social_avatar_url = pic
            update_fields.append("social_avatar_url")
        if update_fields:
            existing.save(update_fields=update_fields)
        return existing, False

    if email:
        by_email = User.objects.filter(email__iexact=email).exclude(email="").first()
        if by_email:
            by_email.social_provider = sp
            by_email.social_provider_id = provider_user_id
            if name and (not by_email.name or by_email.name == by_email.phone):
                by_email.name = name[:150]
            if pic:
                by_email.social_avatar_url = pic
            # Mirror legacy behaviour: include ``name`` whenever the provider sent a non-empty name.
            uf: list[str] = ["social_provider", "social_provider_id"]
            if name:
                uf.append("name")
            if pic:
                uf.append("social_avatar_url")
            by_email.save(update_fields=uf)
            return by_email, False

    phone = _allocate_placeholder_phone()
    display_name = (name or email or "User")[:150]
    prefix = "g_" if provider == "google" else "fb_"
    uid_clean = "".join(c for c in provider_user_id if c.isalnum())
    base_username = (prefix + uid_clean)[:50]
    username = base_username
    n = 0
    while User.objects.filter(username=username).exists():
        n += 1
        suffix = f"_{n}"
        username = (base_username[: 50 - len(suffix)] + suffix)[:50]

    user = User(
        name=display_name,
        phone=phone,
        username=username,
        email=email[:254] if email else "",
        social_provider=sp,
        social_provider_id=provider_user_id,
        social_avatar_url=pic,
        role=User.Role.NORMAL,
        oauth_phone_completed=False,
    )
    user.set_unusable_password()
    user.save()
    return user, True


def _maybe_provision_family_for_new_oauth_user(user: User, next_path: str) -> str | None:
    """If new user signed up via family-portal OAuth next path, create their family group. Returns error message or None."""
    if infer_portal_key_from_frontend_path(next_path) != PORTAL_FAMILY:
        return None
    try:
        with transaction.atomic():
            family_service.create_family_group_for_user(user, f"{user.name}'s Family")
    except ValueError as e:
        return str(e)
    return None


def _http_post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        logger.warning("OAuth token HTTP error: %s %s", e.code, raw[:500])
        raise
    return json.loads(raw) if raw else {}


def _http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else {}


def _default_oauth_next_path() -> str:
    return getattr(settings, "REDIRECT_AFTER_LOGIN", "/portal").strip() or "/portal"


@csrf_exempt
def google_oauth_start(request):
    next_path = (request.GET.get("next") or "").strip() or _default_oauth_next_path()
    error_return = _normalize_error_return_path(request.GET.get("return_error"), "/login")
    flow = (request.GET.get("flow") or "login").strip().lower()
    if flow not in ("login", "register"):
        flow = "login"
    cid = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") or ""
    if not cid:
        return _redirect_to_frontend({"oauth_error": _GOOGLE_OAUTH_NOT_CONFIGURED}, error_return)
    state = _sign_state("google", next_path, error_return, flow)
    redirect_uri = f"{_public_api_base(request)}{reverse('oauth-google-callback')}"
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return HttpResponseRedirect(url)


@csrf_exempt
def google_oauth_callback(request):
    error = request.GET.get("error")
    if error:
        return _redirect_to_frontend(
            {"oauth_error": request.GET.get("error_description") or error}
        )
    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code or not state:
        return _redirect_to_frontend({"oauth_error": "Missing OAuth code."})
    try:
        provider, next_path, error_return, _oauth_flow = _read_state(state)
        if provider != "google":
            raise signing.BadSignature("state")
    except signing.BadSignature:
        return _redirect_to_frontend({"oauth_error": "Invalid or expired OAuth state."})

    secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "") or ""
    cid = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") or ""
    if not secret or not cid:
        return _redirect_to_frontend({"oauth_error": _GOOGLE_OAUTH_NOT_CONFIGURED}, error_return)

    redirect_uri = f"{_public_api_base(request)}{reverse('oauth-google-callback')}"
    try:
        token_payload = _http_post_form(
            "https://oauth2.googleapis.com/token",
            {
                "code": code,
                "client_id": cid,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        logger.exception("Google token exchange failed")
        return _redirect_to_frontend(
            {"oauth_error": "Token exchange failed."}, error_return
        )

    access = token_payload.get("access_token")
    if not access:
        return _redirect_to_frontend(
            {"oauth_error": "No access token from Google."}, error_return
        )

    try:
        req = urllib.request.Request("https://www.googleapis.com/oauth2/v2/userinfo")
        req.add_header("Authorization", f"Bearer {access}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
        profile = json.loads(raw) if raw else {}
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.exception("Google userinfo failed")
        return _redirect_to_frontend(
            {"oauth_error": "Could not load Google profile."}, error_return
        )

    gid = str(profile.get("id") or "")
    if not gid:
        return _redirect_to_frontend(
            {"oauth_error": "Invalid Google profile."}, error_return
        )

    name = (profile.get("name") or profile.get("email") or "")[:150]
    email = (profile.get("email") or "")[:254]
    picture = profile.get("picture") or ""
    user, created_new = _get_or_create_social_user(
        "google", gid, name, email, avatar_url=str(picture) if picture else ""
    )
    if not user.is_active:
        return _redirect_to_frontend({"oauth_error": "Account disabled."}, error_return)
    if created_new:
        err = _maybe_provision_family_for_new_oauth_user(user, next_path)
        if err:
            return _redirect_to_frontend({"oauth_error": err}, error_return)
        get_or_create_personal_wallet(user)

    if not user.oauth_phone_completed:
        pending = sign_oauth_pending_token(user.pk, next_path)
        return _redirect_to_frontend({"oauth_pending": pending}, error_return)

    requested_portal = infer_portal_key_from_frontend_path(next_path)
    portal_key = _effective_portal_key_for_oauth(user, next_path)
    data = build_auth_response_for_portal(user, portal_key)
    if isinstance(data, Response):
        detail = getattr(data, "data", None) or {}
        msg = detail.get("detail", "Sign-in not allowed for this portal.")
        return _redirect_to_frontend({"oauth_error": str(msg)}, error_return)
    redirect_final = data["redirect"]
    if portal_key == requested_portal and next_path.startswith("/") and not next_path.startswith("//"):
        # Prefer server canonical `/portal` over legacy hardcoded `/portal/dashboard` when equivalent.
        legacy_dashboard = next_path.strip() == "/portal/dashboard"
        canonical = str(redirect_final or "").rstrip("/") == "/portal"
        if not (legacy_dashboard and canonical):
            redirect_final = next_path
    return _redirect_to_frontend(
        {
            "token": data["token"],
            "surface": data["surface"],
            "redirect": redirect_final,
        },
        error_return,
    )


@csrf_exempt
def facebook_oauth_start(request):
    next_path = (request.GET.get("next") or "").strip() or _default_oauth_next_path()
    error_return = _normalize_error_return_path(request.GET.get("return_error"), "/login")
    flow = (request.GET.get("flow") or "login").strip().lower()
    if flow not in ("login", "register"):
        flow = "login"
    app_id = getattr(settings, "FACEBOOK_APP_ID", "") or ""
    if not app_id:
        return _redirect_to_frontend(
            {"oauth_error": "Facebook OAuth is not configured."}, error_return
        )
    state = _sign_state("facebook", next_path, error_return, flow)
    redirect_uri = f"{_public_api_base(request)}{reverse('oauth-facebook-callback')}"
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "email,public_profile",
    }
    url = "https://www.facebook.com/v18.0/dialog/oauth?" + urllib.parse.urlencode(params)
    return HttpResponseRedirect(url)


@csrf_exempt
def facebook_oauth_callback(request):
    error = request.GET.get("error")
    if error:
        return _redirect_to_frontend({"oauth_error": error})
    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code or not state:
        return _redirect_to_frontend({"oauth_error": "Missing OAuth code."})
    try:
        provider, next_path, error_return, _oauth_flow = _read_state(state)
        if provider != "facebook":
            raise signing.BadSignature("state")
    except signing.BadSignature:
        return _redirect_to_frontend({"oauth_error": "Invalid or expired OAuth state."})

    app_id = getattr(settings, "FACEBOOK_APP_ID", "") or ""
    app_secret = getattr(settings, "FACEBOOK_APP_SECRET", "") or ""
    if not app_id or not app_secret:
        return _redirect_to_frontend(
            {"oauth_error": "Facebook OAuth is not configured."}, error_return
        )

    redirect_uri = f"{_public_api_base(request)}{reverse('oauth-facebook-callback')}"
    token_url = (
        "https://graph.facebook.com/v18.0/oauth/access_token?"
        + urllib.parse.urlencode(
            {
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            }
        )
    )
    try:
        token_payload = _http_get_json(token_url)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.exception("Facebook token exchange failed")
        return _redirect_to_frontend(
            {"oauth_error": "Token exchange failed."}, error_return
        )

    access = token_payload.get("access_token")
    if not access:
        return _redirect_to_frontend(
            {"oauth_error": "No access token from Facebook."}, error_return
        )

    me_url = (
        "https://graph.facebook.com/me?"
        + urllib.parse.urlencode(
            {"fields": "id,name,email", "access_token": access}
        )
    )
    try:
        profile = _http_get_json(me_url)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.exception("Facebook me failed")
        return _redirect_to_frontend(
            {"oauth_error": "Could not load Facebook profile."}, error_return
        )

    fid = str(profile.get("id") or "")
    if not fid:
        return _redirect_to_frontend(
            {"oauth_error": "Invalid Facebook profile."}, error_return
        )

    name = (profile.get("name") or profile.get("email") or "")[:150]
    email = (profile.get("email") or "")[:254]
    user, created_new = _get_or_create_social_user("facebook", fid, name, email)
    if not user.is_active:
        return _redirect_to_frontend({"oauth_error": "Account disabled."}, error_return)
    if created_new:
        err = _maybe_provision_family_for_new_oauth_user(user, next_path)
        if err:
            return _redirect_to_frontend({"oauth_error": err}, error_return)
        get_or_create_personal_wallet(user)

    if not user.oauth_phone_completed:
        pending = sign_oauth_pending_token(user.pk, next_path)
        return _redirect_to_frontend({"oauth_pending": pending}, error_return)

    requested_portal = infer_portal_key_from_frontend_path(next_path)
    portal_key = _effective_portal_key_for_oauth(user, next_path)
    data = build_auth_response_for_portal(user, portal_key)
    if isinstance(data, Response):
        detail = getattr(data, "data", None) or {}
        msg = detail.get("detail", "Sign-in not allowed for this portal.")
        return _redirect_to_frontend({"oauth_error": str(msg)}, error_return)
    redirect_final = data["redirect"]
    if portal_key == requested_portal and next_path.startswith("/") and not next_path.startswith("//"):
        legacy_dashboard = next_path.strip() == "/portal/dashboard"
        canonical = str(redirect_final or "").rstrip("/") == "/portal"
        if not (legacy_dashboard and canonical):
            redirect_final = next_path
    return _redirect_to_frontend(
        {
            "token": data["token"],
            "surface": data["surface"],
            "redirect": redirect_final,
        },
        error_return,
    )
