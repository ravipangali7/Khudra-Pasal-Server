from django.core.management.base import BaseCommand

from core.models import NavigationItem
from core.nav_seed import all_seed_rows


class Command(BaseCommand):
    help = "Populate NavigationItem rows from nav_seed (upserts by surface+key)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete existing navigation rows and re-seed.",
        )

    def handle(self, *args, **options):
        force = options["force"]
        if force:
            deleted, _ = NavigationItem.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} navigation rows."))
        seed_rows = list(all_seed_rows())

        created = 0
        updated = 0
        for row in seed_rows:
            (
                surface,
                key,
                label,
                icon,
                parent_key,
                sort_order,
                badge_key,
                roles_filter,
                view_key,
            ) = row
            _obj, was_created = NavigationItem.objects.update_or_create(
                surface=surface,
                key=key,
                defaults={
                    "label": label,
                    "icon": icon,
                    "parent_key": parent_key or "",
                    "sort_order": sort_order,
                    "badge_key": badge_key or "",
                    "roles_filter": roles_filter or "",
                    "view_key": (view_key or "").strip(),
                },
            )
            if was_created:
                created += 1
            else:
                updated += 1

        # Remove deprecated vendor nav keys dropped from seeds (Marketing, coupons, flash-deals, etc.).
        seed_vendor_keys = {k for (s, k, *_rest) in seed_rows if s == "vendor"}
        deprecated_vendor_keys = {"logout", "marketing", "coupons", "flash-deals", "faq", "settings"}
        to_remove = sorted(deprecated_vendor_keys - seed_vendor_keys)
        if to_remove:
            deleted, _ = NavigationItem.objects.filter(surface="vendor", key__in=to_remove).delete()
            if deleted:
                self.stdout.write(
                    self.style.WARNING(
                        f"Deleted {deleted} deprecated vendor nav row(s): {', '.join(to_remove)}"
                    )
                )

        # Remove deprecated family portal nav keys dropped from seeds.
        seed_family_keys = {k for (s, k, *_rest) in seed_rows if s == "portal_family"}
        deprecated_family_keys = {
            "members-add",
            "settings",
            "wallets",
            "wallets-load",
            "wallets-transfer",
        }
        to_remove_family = sorted(deprecated_family_keys - seed_family_keys)
        if to_remove_family:
            deleted, _ = NavigationItem.objects.filter(
                surface="portal_family", key__in=to_remove_family
            ).delete()
            if deleted:
                self.stdout.write(
                    self.style.WARNING(
                        f"Deleted {deleted} deprecated family portal nav rows: {', '.join(to_remove_family)}"
                    )
                )

        self.stdout.write(self.style.SUCCESS(f"Navigation seeded. Created {created}, updated {updated}."))
