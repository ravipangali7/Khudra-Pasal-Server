from core.models import CMSPage
from core.seo.resolvers import spa_url


def entries():
    qs = CMSPage.objects.filter(status=CMSPage.Status.PUBLISHED).only("slug", "last_updated")
    for row in qs:
        lm = row.last_updated.date().isoformat() if row.last_updated else ""
        yield {
            "loc": spa_url(f"/page/{row.slug}"),
            "lastmod": lm,
            "changefreq": "monthly",
            "priority": "0.6",
        }
