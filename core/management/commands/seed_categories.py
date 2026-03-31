"""Seed a realistic 3-level category tree for Nepal grocery / general retail."""
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from core.models import Category


def _cat(name, icon="", parent=None, level=0, sort_order=0):
    slug_base = slugify(name)[:110]
    slug = slug_base
    n = 0
    while Category.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{slug_base}-{n}"[:120]
    return Category.objects.create(
        name=name,
        slug=slug,
        icon=icon,
        parent=parent,
        level=level,
        sort_order=sort_order,
        status=Category.Status.ACTIVE,
    )


class Command(BaseCommand):
    help = "Seed ~20+ categories (3 levels) with icons."

    def handle(self, *args, **options):
        if Category.objects.exists():
            self.stdout.write(
                self.style.WARNING("Categories already exist; skipping to avoid duplicates.")
            )
            return

        order = 0

        veg = _cat("Vegetables", "🥬", None, 0, order)
        order += 1
        leafy = _cat("Leafy Greens", "🥬", veg, 1, 0)
        _cat("Spinach", "🌿", leafy, 2, 0)
        _cat("Mustard Greens", "🌿", leafy, 2, 1)
        roots = _cat("Root Vegetables", "🥕", veg, 1, 1)
        _cat("Potato", "🥔", roots, 2, 0)
        _cat("Onion", "🧅", roots, 2, 1)

        dairy = _cat("Dairy & Eggs", "🥛", None, 0, order)
        order += 1
        milk = _cat("Milk", "🥛", dairy, 1, 0)
        _cat("Full Cream Milk", "", milk, 2, 0)
        _cat("Skimmed Milk", "", milk, 2, 1)
        _cat("Yogurt & Curd", "🍶", dairy, 1, 1)

        grains = _cat("Rice & Grains", "🍚", None, 0, order)
        order += 1
        _cat("Basmati Rice", "", grains, 1, 0)
        _cat("Local Jeera Masino", "", grains, 1, 1)

        snacks = _cat("Snacks & Biscuits", "🍪", None, 0, order)
        order += 1
        _cat("Instant Noodles", "🍜", snacks, 1, 0)
        _cat("Chips & Namkeen", "", snacks, 1, 1)

        bev = _cat("Beverages", "☕", None, 0, order)
        order += 1
        _cat("Tea & Coffee", "☕", bev, 1, 0)
        _cat("Soft Drinks", "🥤", bev, 1, 1)

        elec = _cat("Electronics", "📱", None, 0, order)
        order += 1
        mobile = _cat("Mobile", "📱", elec, 1, 0)
        _cat("Smartphones", "", mobile, 2, 0)
        _cat("Accessories", "🔌", mobile, 2, 1)

        home = _cat("Home & Kitchen", "🏠", None, 0, order)
        order += 1
        _cat("Cookware", "🍳", home, 1, 0)
        _cat("Storage", "📦", home, 1, 1)

        personal = _cat("Personal Care", "🧴", None, 0, order)
        order += 1
        _cat("Hair Care", "", personal, 1, 0)
        _cat("Oral Care", "", personal, 1, 1)

        baby = _cat("Baby Care", "👶", None, 0, order)
        order += 1
        _cat("Diapers", "", baby, 1, 0)

        pet = _cat("Pet Supplies", "🐕", None, 0, order)
        order += 1
        _cat("Dog Food", "", pet, 1, 0)

        count = Category.objects.count()
        self.stdout.write(self.style.SUCCESS(f"Seeded {count} categories."))
