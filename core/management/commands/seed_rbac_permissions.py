from django.core.management.base import BaseCommand

from core.rbac_django import seed_rbac_permissions


class Command(BaseCommand):
    help = "Create Django Permission rows for portal RBAC (Group role permissions)."

    def handle(self, *args, **options):
        created, updated = seed_rbac_permissions()
        self.stdout.write(
            self.style.SUCCESS(
                f"RBAC permissions seeded. created={created}, updated={updated}"
            )
        )
