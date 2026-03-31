"""Seed built-in employee roles (system roles, JSON permissions)."""
from django.core.management.base import BaseCommand

from core.models import Role


PERMISSIONS = {
    "Super Admin": {
        "products": True,
        "orders": True,
        "cms": True,
        "marketing": True,
        "finance": True,
        "wallet-master": True,
        "reports": True,
        "users": True,
        "sellers": True,
        "families": True,
        "settings": True,
        "security": True,
        "employees": True,
    },
    "Staff": {
        "products": True,
        "orders": True,
        "cms": True,
        "marketing": False,
        "finance": False,
        "wallet-master": False,
        "reports": False,
        "users": False,
        "sellers": True,
        "families": False,
        "settings": False,
        "security": False,
        "employees": False,
    },
    "Finance": {
        "products": False,
        "orders": True,
        "cms": False,
        "marketing": False,
        "finance": True,
        "wallet-master": True,
        "reports": True,
        "users": False,
        "sellers": True,
        "families": False,
        "settings": False,
        "security": False,
        "employees": False,
    },
    "Moderator": {
        "products": False,
        "orders": False,
        "cms": True,
        "marketing": True,
        "finance": False,
        "wallet-master": False,
        "reports": False,
        "users": False,
        "sellers": False,
        "families": False,
        "settings": False,
        "security": False,
        "employees": False,
    },
    "Viewer": {
        "products": True,
        "orders": True,
        "cms": True,
        "marketing": True,
        "finance": True,
        "wallet-master": True,
        "reports": True,
        "users": True,
        "sellers": True,
        "families": True,
        "settings": True,
        "security": True,
        "employees": True,
    },
}


class Command(BaseCommand):
    help = "Create or update 5 system Role rows (Super Admin, Staff, Finance, Moderator, Viewer)."

    def handle(self, *args, **options):
        for name, perms in PERMISSIONS.items():
            role, created = Role.objects.update_or_create(
                name=name,
                defaults={
                    "permissions": perms,
                    "is_system": True,
                    "status": Role.Status.ACTIVE,
                },
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(f"  {verb}: {role.name}")

        self.stdout.write(self.style.SUCCESS(f"Roles synced: {Role.objects.filter(is_system=True).count()}"))
