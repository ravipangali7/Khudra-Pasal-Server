from rest_framework.throttling import SimpleRateThrottle


class FamilyPortalJoinThrottle(SimpleRateThrottle):
    """Rate limit by IP for public family join link GET/POST."""

    scope = "family_join"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
