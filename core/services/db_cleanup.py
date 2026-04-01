"""
Super-admin database cleanup: module registry and transactional execution.

Module list and delete order are defined only here — the API exposes them dynamically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Model

from core.models import (
    Attribute,
    AttributeValue,
    AuditLog,
    Banner,
    Brand,
    Cart,
    CartItem,
    Category,
    Coupon,
    DeliveryAddress,
    FlashDeal,
    FlashDealProduct,
    KYCDocument,
    Notification,
    Order,
    OrderCommissionSettlement,
    OrderItem,
    OTPVerification,
    PaymentTransaction,
    Product,
    ProductApproval,
    ProductImage,
    ProductReview,
    ProductWishlist,
    PurchaseApprovalRequest,
    PurchaseOrder,
    PurchaseOrderLine,
    Refund,
    Reel,
    Role,
    EmployeeProfile,
    SupportTicket,
    SupportTicketMessage,
    SupportTicketMessageAttachment,
    Unit,
)

User = get_user_model()


@dataclass(frozen=True)
class CleanupModuleDef:
    id: str
    label: str
    description: str
    sort_order: int
    models: tuple[type[Model], ...]
    """Models deleted in order via QuerySet.delete()."""
    extra_deleters: tuple[Callable[[], int], ...] = ()
    """Run before `models` deletes (e.g. filtered querysets). Return rows deleted."""


def _delete_order_linked_payment_transactions() -> int:
    n, _ = PaymentTransaction.objects.filter(order__isnull=False).delete()
    return n


# Lower sort_order runs first when multiple modules are selected.
_CLEANUP_MODULES_LIST: list[CleanupModuleDef] = [
    CleanupModuleDef(
        id="orders",
        label="Orders & payments (order-linked)",
        description="Refunds, settlements, payment rows tied to an order, then orders and line items.",
        sort_order=10,
        models=(Order,),  # cascades OrderItem, DeliveryAddress; prefaced by extra_deleters
        extra_deleters=(
            lambda: Refund.objects.all().delete()[0],
            lambda: OrderCommissionSettlement.objects.all().delete()[0],
            _delete_order_linked_payment_transactions,
        ),
    ),
    CleanupModuleDef(
        id="purchase_orders",
        label="Purchase orders (B2B)",
        description="PO lines and purchase orders (run before catalog if products are selected).",
        sort_order=15,
        models=(PurchaseOrderLine, PurchaseOrder),
    ),
    CleanupModuleDef(
        id="marketing",
        label="Marketing",
        description="Flash deals (through table), coupons, and banners.",
        sort_order=20,
        models=(FlashDealProduct, FlashDeal, Coupon, Banner),
    ),
    CleanupModuleDef(
        id="carts",
        label="Shopping carts",
        description="Cart line items and carts (users are not removed).",
        sort_order=25,
        models=(CartItem, Cart),
    ),
    CleanupModuleDef(
        id="reels",
        label="Reels",
        description="Reel engagement data and reels.",
        sort_order=28,
        models=(Reel,),  # cascades interactions, views, comments
    ),
    CleanupModuleDef(
        id="products",
        label="Products",
        description="Wishlists, reviews, approvals, gallery images, purchase approval requests, products.",
        sort_order=40,
        models=(
            ProductWishlist,
            ProductReview,
            ProductApproval,
            ProductImage,
            PurchaseApprovalRequest,
            Product,
        ),
    ),
    CleanupModuleDef(
        id="catalog_meta",
        label="Catalog taxonomy",
        description="Attribute values, attributes, units, brands, categories.",
        sort_order=50,
        models=(AttributeValue, Attribute, Unit, Brand, Category),
    ),
    CleanupModuleDef(
        id="audit_logs",
        label="Audit logs",
        description="Admin audit trail entries.",
        sort_order=60,
        models=(AuditLog,),
    ),
    CleanupModuleDef(
        id="notifications",
        label="In-app notifications",
        description="Stored notifications for users (accounts are preserved).",
        sort_order=65,
        models=(Notification,),
    ),
    CleanupModuleDef(
        id="support_tickets",
        label="Support tickets",
        description="Ticket attachments, messages, and tickets.",
        sort_order=70,
        models=(
            SupportTicketMessageAttachment,
            SupportTicketMessage,
            SupportTicket,
        ),
    ),
]

CLEANUP_MODULES: dict[str, CleanupModuleDef] = {m.id: m for m in _CLEANUP_MODULES_LIST}

FORBIDDEN_MODULE_IDS: frozenset[str] = frozenset(
    {
        "users",
        "user",
        "staff",
        "employees",
        "auth",
        "accounts",
        "kyc",
        "wallets",
        "wallet",
        "vendors",
        "families",
        "family",
    }
)

_PROTECTED_MODELS: frozenset[type[Model]] = frozenset(
    {
        User,
        EmployeeProfile,
        Role,
        OTPVerification,
        KYCDocument,
    }
)


def assert_registry_safety() -> None:
    for mod in _CLEANUP_MODULES_LIST:
        for model in mod.models:
            if model in _PROTECTED_MODELS:
                raise RuntimeError(f"Cleanup module {mod.id!r} includes protected model {model!r}")


def list_cleanup_modules_payload() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for mod in sorted(_CLEANUP_MODULES_LIST, key=lambda m: (m.sort_order, m.id)):
        approx = 0
        if mod.id == "orders":
            try:
                approx = (
                    Refund.objects.count()
                    + OrderCommissionSettlement.objects.count()
                    + PaymentTransaction.objects.filter(order__isnull=False).count()
                    + Order.objects.count()
                    + OrderItem.objects.count()
                    + DeliveryAddress.objects.count()
                )
            except Exception:
                pass
        else:
            for model in mod.models:
                try:
                    approx += model.objects.count()
                except Exception:
                    pass
        out.append(
            {
                "id": mod.id,
                "label": mod.label,
                "description": mod.description,
                "approximate_row_count": approx,
            }
        )
    return out


def validate_module_ids(raw: list[Any]) -> tuple[list[str] | None, str | None]:
    if not isinstance(raw, list):
        return None, "module_ids must be a list."
    ids: list[str] = []
    for x in raw:
        if not isinstance(x, str) or not x.strip():
            return None, "Each module id must be a non-empty string."
        ids.append(x.strip())
    deduped: list[str] = []
    for i in ids:
        if i not in deduped:
            deduped.append(i)
    if not deduped:
        return None, "Select at least one module."
    for i in deduped:
        if i in FORBIDDEN_MODULE_IDS:
            return None, f"Module {i!r} is not allowed for cleanup."
        if i not in CLEANUP_MODULES:
            return None, f"Unknown module: {i!r}."
    return deduped, None


@transaction.atomic
def run_cleanup(module_ids: list[str]) -> list[dict[str, Any]]:
    """Delete data for each module in global sort_order. Rolls back on any error."""
    ordered = sorted(module_ids, key=lambda mid: CLEANUP_MODULES[mid].sort_order)
    results: list[dict[str, Any]] = []
    for mid in ordered:
        mod = CLEANUP_MODULES[mid]
        total_deleted = 0
        for deleter in mod.extra_deleters:
            total_deleted += deleter()
        for model in mod.models:
            n, _ = model.objects.all().delete()
            total_deleted += n
        results.append({"id": mid, "rows_deleted": total_deleted})
    return results


assert_registry_safety()
