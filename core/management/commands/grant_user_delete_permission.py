"""
Grant Django delete permission for core.User to trusted staff accounts.

Default behavior:
- grants `core.delete_user` (and `core.view_user`) to staff users with role SUPER_ADMIN
- skips superusers because they already have full permissions
"""

from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand

from core.models import User


class Command(BaseCommand):
    help = "Grant core.delete_user permission to trusted staff users."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            type=str,
            default="",
            help="Grant permission to a specific username only.",
        )

    def handle(self, *args, **options):
        delete_perm = Permission.objects.get(codename="delete_user", content_type__app_label="core")
        view_perm = Permission.objects.get(codename="view_user", content_type__app_label="core")

        username = (options.get("username") or "").strip()
        if username:
            qs = User.objects.filter(username=username, is_staff=True, is_superuser=False)
        else:
            qs = User.objects.filter(
                is_staff=True,
                is_superuser=False,
                role=User.Role.SUPER_ADMIN,
            )

        granted = 0
        for user in qs:
            user.user_permissions.add(delete_perm, view_perm)
            granted += 1

        if username and granted == 0:
            self.stdout.write(
                self.style.WARNING(
                    f'No eligible non-superuser staff found for username "{username}".'
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Granted core.view_user/core.delete_user to {granted} user(s)."
            )
        )
