"""SMTP-backed mail: skip when unconfigured, order emails after commit, KYC status emails."""

import os
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase

from core.models import (
    Category,
    KYCDocument,
    Order,
    OrderItem,
    Product,
    SiteSettings,
    User,
    Vendor,
)
from core.services import mail_service
from core.services.vendor_service import ensure_vendor_wallet


class MailServiceConfigTests(TestCase):
    def test_send_html_skips_without_smtp_host(self):
        site = SiteSettings.load()
        site.smtp_host = ""
        site.save(update_fields=["smtp_host"])
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KP_SMTP_HOST", None)
            with patch("core.services.mail_service.EmailMultiAlternatives.send") as mock_send:
                mail_service.send_html_email("Hi", "<p>x</p>", ["a@b.com"], site=site)
                mock_send.assert_not_called()

    def test_effective_smtp_host_env_overrides_db(self):
        site = SiteSettings.load()
        site.smtp_host = "db.example.com"
        with patch.dict(os.environ, {"KP_SMTP_HOST": "env.example.com"}):
            self.assertEqual(mail_service.effective_smtp_host(site), "env.example.com")
        self.assertEqual(mail_service.effective_smtp_host(site), "db.example.com")

    @patch("core.services.mail_service.EmailMultiAlternatives.send")
    def test_send_html_uses_kp_smtp_host_when_db_empty(self, mock_send):
        site = SiteSettings.load()
        site.smtp_host = ""
        site.smtp_port = 587
        site.smtp_username = "user@test.local"
        site.smtp_password = "secret"
        site.save(
            update_fields=["smtp_host", "smtp_port", "smtp_username", "smtp_password"]
        )
        with patch.dict(os.environ, {"KP_SMTP_HOST": "smtp.override.local"}):
            mail_service.send_html_email("Hi", "<p>x</p>", ["a@b.com"], site=site)
        mock_send.assert_called_once()

    @patch("core.management.commands.test_smtp.send_html_email")
    def test_management_test_smtp_command(self, mock_send_html):
        site = SiteSettings.load()
        site.smtp_host = "smtp.test.local"
        site.site_email = "admin-check@test.local"
        site.save(update_fields=["smtp_host", "site_email"])
        call_command("test_smtp", verbosity=0)
        mock_send_html.assert_called_once()
        _args, kwargs = mock_send_html.call_args
        self.assertIn("raise_exceptions", kwargs)
        self.assertTrue(kwargs["raise_exceptions"])


class OrderPlacedEmailTests(TestCase):
    def setUp(self):
        self.site = SiteSettings.load()
        self.site.smtp_host = "smtp.test.local"
        self.site.smtp_port = 587
        self.site.site_email = "admin@test.local"
        self.site.smtp_from_email = "noreply@test.local"
        self.site.save()

        self.vendor_user = User.objects.create_user(
            username="m_vendor",
            password="x",
            phone="9811111111",
            name="Vendor",
            email="vendor@test.local",
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Mail Store",
            store_slug="mail-store-smtp",
            status=Vendor.Status.APPROVED,
            contact_email="store@test.local",
            portal_email_notifications=True,
        )
        ensure_vendor_wallet(self.vendor)

        self.customer = User.objects.create_user(
            username="m_customer",
            password="x",
            phone="9822222222",
            name="Buyer",
            email="buyer@test.local",
        )
        self.cat = Category.objects.create(name="MailCat", slug="mail-cat")
        self.product = Product.objects.create(
            name="Mail Product",
            slug="mail-product",
            sku="SKU-MAIL-1",
            category=self.cat,
            seller=self.vendor,
            type=Product.Type.PHYSICAL,
            price=Decimal("25.00"),
            stock=10,
            status=Product.Status.ACTIVE,
        )

    @patch("core.services.mail_service.EmailMultiAlternatives.send")
    def test_order_on_commit_sends_after_line_items(self, mock_send):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                order = Order.objects.create(
                    order_number="MAIL-ORD-1",
                    customer=self.customer,
                    seller=self.vendor,
                    status=Order.Status.PENDING,
                    payment_method=Order.PaymentMethod.COD,
                    payment_status=Order.PaymentStatus.PENDING,
                    subtotal=Decimal("25.00"),
                    delivery_fee=Decimal("0"),
                    discount_amount=Decimal("0"),
                    total=Decimal("25.00"),
                    want_delivery=False,
                )
                OrderItem.objects.create(
                    order=order,
                    product=self.product,
                    quantity=1,
                    list_unit_price=Decimal("25.00"),
                    unit_price=Decimal("25.00"),
                    total_price=Decimal("25.00"),
                )

        self.assertGreaterEqual(mock_send.call_count, 1)


class KycStatusEmailTests(TestCase):
    def setUp(self):
        site = SiteSettings.load()
        site.smtp_host = "smtp.test.local"
        site.smtp_port = 587
        site.smtp_from_email = "noreply@test.local"
        site.save()

    @patch("core.services.mail_service.EmailMultiAlternatives.send")
    def test_user_kyc_change_triggers_email(self, mock_send):
        user = User.objects.create_user(
            username="kyc_mail_u",
            password="x",
            phone="9833333333",
            name="KYC Mail",
            email="kycuser@test.local",
            kyc_status=User.KYCStatus.PENDING,
        )
        with self.captureOnCommitCallbacks(execute=True):
            user.kyc_status = User.KYCStatus.VERIFIED
            user.save(update_fields=["kyc_status"])
        self.assertGreaterEqual(mock_send.call_count, 1)

    @patch("core.services.mail_service.EmailMultiAlternatives.send")
    def test_kyc_document_sync_triggers_email(self, mock_send):
        user = User.objects.create_user(
            username="kyc_doc_mail",
            password="x",
            phone="9844444444",
            name="KYC Doc",
            email="kycdoc@test.local",
            kyc_status=User.KYCStatus.PENDING,
        )
        with self.captureOnCommitCallbacks(execute=True):
            KYCDocument.objects.create(
                user=user,
                document_type=KYCDocument.DocumentType.CITIZENSHIP,
                status=KYCDocument.Status.APPROVED,
            )
        user.refresh_from_db()
        self.assertEqual(user.kyc_status, User.KYCStatus.VERIFIED)
        self.assertGreaterEqual(mock_send.call_count, 1)
