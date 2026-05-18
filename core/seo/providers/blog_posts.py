from core.models import BlogPost
from core.seo.resolvers import spa_url


def entries():
    qs = BlogPost.objects.filter(status=BlogPost.Status.PUBLISHED).only(
        "slug", "published_at", "created_at"
    )
    for row in qs:
        dt = row.published_at or row.created_at
        lm = dt.date().isoformat() if dt else ""
        yield {
            "loc": spa_url(f"/blog/{row.slug}"),
            "lastmod": lm,
            "changefreq": "weekly",
            "priority": "0.7",
        }
