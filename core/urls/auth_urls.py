from django.urls import path

from core.views.auth_otp import otp_send, otp_verify
from core.views.social_oauth import (
    facebook_oauth_callback,
    facebook_oauth_start,
    google_oauth_callback,
    google_oauth_start,
)
from core.views.unified_auth import unified_login

urlpatterns = [
    path("login/", unified_login, name="unified-login"),
    path("otp/send/", otp_send, name="auth-otp-send"),
    path("otp/verify/", otp_verify, name="auth-otp-verify"),
    path("social/google/start/", google_oauth_start, name="oauth-google-start"),
    path("social/google/callback/", google_oauth_callback, name="oauth-google-callback"),
    path("social/facebook/start/", facebook_oauth_start, name="oauth-facebook-start"),
    path("social/facebook/callback/", facebook_oauth_callback, name="oauth-facebook-callback"),
]
