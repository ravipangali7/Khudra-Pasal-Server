from django.utils import timezone

from core.seo.resolvers import public_site_url, spa_url


def entries():
    base = public_site_url()
    if not base:
        return []
    today = timezone.now().date().isoformat()
    static = [
        ("/", "daily", "1.0"),
        ("/products", "daily", "0.9"),
        ("/blog", "weekly", "0.8"),
        ("/brands", "weekly", "0.7"),
    ]
    for path, freq, pri in static:
        yield {
            "loc": spa_url(path),
            "lastmod": today,
            "changefreq": freq,
            "priority": pri,
        }
