"""Rules for which products appear on the storefront and may be added to cart.

Keep in sync with cart, wishlist, checkout pricing, and reel commerce affordances.
"""

from django.db.models import Q

from core.models import Product, Vendor


def storefront_active_product_q() -> Q:
    """ORM filter matching purchasable website catalog products."""
    return Q(status=Product.Status.ACTIVE) & (
        Q(seller__isnull=True) | Q(seller__status=Vendor.Status.APPROVED)
    )


def product_is_storefront_purchasable(product: Product) -> bool:
    """Python-side check for a single loaded ``Product`` (use when not using the ORM filter)."""
    if product.status != Product.Status.ACTIVE:
        return False
    if product.seller_id is None:
        return True
    return product.seller.status == Vendor.Status.APPROVED
