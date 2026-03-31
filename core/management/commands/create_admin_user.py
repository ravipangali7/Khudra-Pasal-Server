"""
Create default super admin user (idempotent).
Phone: +977-9800000000, username: admin
Password: env ADMIN_PASSWORD or interactive prompt.
"""
import getpass
import os

from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import User


class Command(BaseCommand):
    help = "Create KhudraPasal super admin (skips if username 'admin' exists)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Reset password if admin user already exists.",
        )

    def handle(self, *args, **options):
        force = options["force"]
        if User.objects.filter(username="admin").exists() and not force:
            self.stdout.write(
                self.style.WARNING(
                    'User with username "admin" already exists. Use --force to set password again.'
                )
            )
            return

        password = os.environ.get("ADMIN_PASSWORD")
        if not password:
            password = getpass.getpass("Admin password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                self.stderr.write(self.style.ERROR("Passwords do not match."))
                return

        phone = "+977-9800000000"
        defaults = {
            "name": "Super Admin",
            "phone": phone,
            "role": User.Role.SUPER_ADMIN,
            "kyc_status": User.KYCStatus.VERIFIED,
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        }

        with transaction.atomic():
            user, created = User.objects.update_or_create(
                username="admin",
                defaults={**defaults, "email": "admin@khudrapasal.local"},
            )
            user.set_password(password)
            # Ensure phone is set (update_or_create may not apply phone if conflict)
            user.phone = phone
            for k, v in defaults.items():
                setattr(user, k, v)
            user.save()

        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} super admin: username=admin phone={phone} role=super_admin"
            )
        )
