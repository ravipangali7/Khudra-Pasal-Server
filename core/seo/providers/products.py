from core.models import Product
from core.seo.resolvers import spa_url
from core.services.storefront_product_visibility import storefront_active_product_q


def entries():
    qs = (
        Product.objects.filter(storefront_active_product_q())
        .only("slug", "updated_at")
        .order_by("-updated_at")[:5000]
    )
    for row in qs:
        lm = row.updated_at.date().isoformat() if row.updated_at else ""
        yield {
            "loc": spa_url(f"/product/{row.slug}"),
            "lastmod": lm,
            "changefreq": "weekly",
            "priority": "0.8",
        }
