from django.urls import path

from core.views.google_auth import GoogleCredentialLoginView
from core.views.auth_otp import otp_send, otp_verify
from core.views.oauth_phone_completion import oauth_phone_send, oauth_phone_verify
from core.views.social_oauth import (
    facebook_oauth_callback,
    facebook_oauth_start,
    google_oauth_callback,
    google_oauth_start,
)
from core.views.device_views import auth_fcm_token
from core.views.unified_auth import auth_session_home, unified_login
from core.views.website import app_promo_views

urlpatterns = [
    path("fcm-token/", auth_fcm_token, name="auth-fcm-token"),
    path(
        "app-promotion-banner/claim-install/",
        app_promo_views.app_promotion_claim_install,
        name="auth-app-promotion-claim-install",
    ),
    path("session-home/", auth_session_home, name="auth-session-home"),
    path("login/", unified_login, name="unified-login"),
    path("google/", GoogleCredentialLoginView.as_view(), name="google-credential-login"),
    path("otp/send/", otp_send, name="auth-otp-send"),
    path("otp/verify/", otp_verify, name="auth-otp-verify"),
    path("social/google/start/", google_oauth_start, name="oauth-google-start"),
    path("social/google/callback/", google_oauth_callback, name="oauth-google-callback"),
    path("social/facebook/start/", facebook_oauth_start, name="oauth-facebook-start"),
    path("social/facebook/callback/", facebook_oauth_callback, name="oauth-facebook-callback"),
    path("social/oauth-phone/send/", oauth_phone_send, name="oauth-phone-send"),
    path("social/oauth-phone/verify/", oauth_phone_verify, name="oauth-phone-verify"),
]
