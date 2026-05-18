from django.utils import timezone

from core.models import Category
from core.seo.resolvers import spa_url


def entries():
    today = timezone.now().date().isoformat()
    for slug in Category.objects.filter(status=Category.Status.ACTIVE).values_list("slug", flat=True):
        yield {
            "loc": spa_url(f"/category/{slug}"),
            "lastmod": today,
            "changefreq": "weekly",
            "priority": "0.7",
        }
