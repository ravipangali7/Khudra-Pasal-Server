from rest_framework.throttling import SimpleRateThrottle


class _SecuritySettingsThrottleMixin:
    """When ip_rate_limiting is off, do not throttle."""

    def allow_request(self, request, view):
        from core.models import SecuritySettings

        if not SecuritySettings.load().ip_rate_limiting:
            return True
        return super().allow_request(request, view)


class FamilyPortalJoinThrottle(_SecuritySettingsThrottleMixin, SimpleRateThrottle):
    """Rate limit by IP for public family join link GET/POST."""

    scope = "family_join"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class OtpSendThrottle(_SecuritySettingsThrottleMixin, SimpleRateThrottle):
    scope = "otp_send"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AdminLoginThrottle(_SecuritySettingsThrottleMixin, SimpleRateThrottle):
    scope = "admin_login"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class WalletHubTransferCodeLookupThrottle(_SecuritySettingsThrottleMixin, SimpleRateThrottle):
    scope = "wallet_hub_lookup"

    def get_cache_key(self, request, view):
        uid = getattr(request.user, "pk", None) or "anon"
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{self.get_ident(request)}:{uid}",
        }
