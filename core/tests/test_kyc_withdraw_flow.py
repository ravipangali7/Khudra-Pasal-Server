"""KYC portal submit, withdraw gating, admin document approval."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    KYCDocument,
    PayoutAccount,
    SiteSettings,
    User,
    Vendor,
    Wallet,
    WalletTransaction,
    WalletWithdrawal,
)
from core.services.base import get_or_create_personal_wallet
from core.services import wallet_service
from core.services.vendor_service import ensure_vendor_wallet
from core.tests.wallet_test_settings import relax_wallet_settings_for_tests


class KycWithdrawFlowTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.customer = User.objects.create_user(
            username="kyc_cust",
            password=self.pw,
            phone="9855555555",
            name="KYC Customer",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.PENDING,
        )
        self.child = User.objects.create_user(
            username="kyc_child",
            password=self.pw,
            phone="9866666666",
            name="KYC Child",
            role=User.Role.CHILD,
            kyc_status=User.KYCStatus.PENDING,
        )
        self.admin = User.objects.create_user(
            username="kyc_admin",
            password=self.pw,
            phone="9877777777",
            name="KYC Admin",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        ss = SiteSettings.load()
        ss.kyc_required = True
        ss.save(update_fields=["kyc_required"])

    def _login(self, user):
        tok, _ = Token.objects.get_or_create(user=user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")

    def _fund_wallet(self, user, amount: Decimal = Decimal("5000")):
        w = get_or_create_personal_wallet(user)
        if w.status != Wallet.Status.ACTIVE:
            w.status = Wallet.Status.ACTIVE
            w.save(update_fields=["status"])
        wallet_service.credit_wallet(
            w,
            amount,
            wtype=WalletTransaction.Type.TOPUP,
            description="test fund",
            performed_by=user,
        )

    def _payout_esewa(self, user: User, phone: str = "9800000000") -> PayoutAccount:
        return PayoutAccount.objects.create(
            user=user,
            type=PayoutAccount.Type.ESEWA,
            phone=phone,
        )

    def test_portal_withdraw_blocked_when_kyc_not_verified(self):
        self._login(self.customer)
        self._fund_wallet(self.customer)
        r = self.client.post(
            "/api/portal/wallet/withdraw/",
            {
                "amount": 100,
                "method_account": "9800000000",
                "bank_name": "Test",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "kyc_required")

    def test_portal_withdraw_blocked_when_site_kyc_disabled_if_not_verified(self):
        ss = SiteSettings.load()
        ss.kyc_required = False
        ss.save(update_fields=["kyc_required"])
        self._login(self.customer)
        self._fund_wallet(self.customer)
        pa = self._payout_esewa(self.customer)
        r = self.client.post(
            "/api/portal/wallet/withdraw/",
            {
                "amount": 100,
                "payout_account_id": pa.pk,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "kyc_required")

    def test_portal_withdraw_allowed_when_verified(self):
        self.customer.kyc_status = User.KYCStatus.VERIFIED
        self.customer.save(update_fields=["kyc_status"])
        self._login(self.customer)
        self._fund_wallet(self.customer)
        pa = self._payout_esewa(self.customer)
        r = self.client.post(
            "/api/portal/wallet/withdraw/",
            {
                "amount": 100,
                "payout_account_id": pa.pk,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertIn("withdrawal_number", r.data)

    def test_portal_withdraw_second_pending_blocked_by_available_balance(self):
        self.customer.kyc_status = User.KYCStatus.VERIFIED
        self.customer.save(update_fields=["kyc_status"])
        self._login(self.customer)
        self._fund_wallet(self.customer, Decimal("100"))
        pa = self._payout_esewa(self.customer)
        r1 = self.client.post(
            "/api/portal/wallet/withdraw/",
            {"amount": 60, "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        r2 = self.client.post(
            "/api/portal/wallet/withdraw/",
            {"amount": 50, "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(WalletWithdrawal.objects.filter(wallet__owner=self.customer).count(), 1)

    def test_portal_withdraw_blocked_without_payout_account_when_kyc_ok(self):
        self.customer.kyc_status = User.KYCStatus.VERIFIED
        self.customer.save(update_fields=["kyc_status"])
        self._login(self.customer)
        self._fund_wallet(self.customer)
        r = self.client.post(
            "/api/portal/wallet/withdraw/",
            {"amount": 50},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "payout_required")

    def test_kyc_submit_creates_pending_document(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self._login(self.customer)
        png = SimpleUploadedFile(
            "id.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00",
            content_type="image/png",
        )
        r = self.client.post(
            "/api/portal/kyc/submit/",
            {
                "document_type": KYCDocument.DocumentType.CITIZENSHIP,
                "document_id_number": "NPL-12345",
                "document_image": png,
            },
            format="multipart",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertTrue(KYCDocument.objects.filter(user=self.customer).exists())
        doc = KYCDocument.objects.get(user=self.customer)
        self.assertEqual(doc.status, KYCDocument.Status.PENDING)
        self.assertEqual(doc.document_id_number, "NPL-12345")

    def test_admin_approve_document_syncs_user_verified(self):
        doc = KYCDocument.objects.create(
            user=self.customer,
            document_type=KYCDocument.DocumentType.CITIZENSHIP,
            status=KYCDocument.Status.PENDING,
        )
        self._login(self.admin)
        r = self.client.patch(
            f"/api/admin/kyc-submissions/{doc.pk}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, User.KYCStatus.VERIFIED)

    def test_admin_approve_passport_syncs_user_verified(self):
        doc = KYCDocument.objects.create(
            user=self.customer,
            document_type=KYCDocument.DocumentType.PASSPORT,
            status=KYCDocument.Status.PENDING,
        )
        self._login(self.admin)
        r = self.client.patch(
            f"/api/admin/kyc-submissions/{doc.pk}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, User.KYCStatus.VERIFIED)

    def test_admin_reject_requires_reason(self):
        doc = KYCDocument.objects.create(
            user=self.customer,
            document_type=KYCDocument.DocumentType.CITIZENSHIP,
            status=KYCDocument.Status.PENDING,
        )
        self._login(self.admin)
        r = self.client.patch(
            f"/api/admin/kyc-submissions/{doc.pk}/",
            {"status": "rejected"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

        r2 = self.client.patch(
            f"/api/admin/kyc-submissions/{doc.pk}/",
            {"status": "rejected", "rejection_reason": "Illegible scan"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        doc.refresh_from_db()
        self.assertEqual(doc.status, KYCDocument.Status.REJECTED)

    def test_admin_kyc_list_includes_enriched_user_and_reviewer(self):
        self.customer.email = "cust@example.com"
        self.customer.save(update_fields=["email"])
        doc = KYCDocument.objects.create(
            user=self.customer,
            document_type=KYCDocument.DocumentType.CITIZENSHIP,
            status=KYCDocument.Status.PENDING,
        )
        self._login(self.admin)
        r = self.client.get("/api/admin/kyc-submissions/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        row = next(x for x in r.data["results"] if x["id"] == str(doc.pk))
        self.assertEqual(row["user"]["email"], "cust@example.com")
        self.assertEqual(row["user"]["username"], self.customer.username)
        self.assertTrue(row["user"]["kid"])
        self.assertIsNone(row["reviewer"])
        r2 = self.client.patch(
            f"/api/admin/kyc-submissions/{doc.pk}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        r3 = self.client.get("/api/admin/kyc-submissions/")
        row2 = next(x for x in r3.data["results"] if x["id"] == str(doc.pk))
        self.assertIsNotNone(row2["reviewer"])
        self.assertEqual(row2["reviewer"]["id"], self.admin.pk)
        self.assertEqual(row2["reviewer"]["name"], self.admin.name)

    def test_portal_kyc_status_can_submit(self):
        self._login(self.customer)
        r = self.client.get("/api/portal/kyc/status/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data["kyc_required"])
        self.assertTrue(r.data["can_submit"])

        self.customer.kyc_status = User.KYCStatus.REVIEW
        self.customer.save(update_fields=["kyc_status"])
        r2 = self.client.get("/api/portal/kyc/status/")
        self.assertFalse(r2.data["can_submit"])

        self.customer.kyc_status = User.KYCStatus.VERIFIED
        self.customer.save(update_fields=["kyc_status"])
        r3 = self.client.get("/api/portal/kyc/status/")
        self.assertFalse(r3.data["can_submit"])

        ss = SiteSettings.load()
        ss.kyc_required = False
        ss.save(update_fields=["kyc_required"])
        self.customer.kyc_status = User.KYCStatus.PENDING
        self.customer.save(update_fields=["kyc_status"])
        r4 = self.client.get("/api/portal/kyc/status/")
        self.assertTrue(r4.data["kyc_required"])
        self.assertTrue(r4.data["can_submit"])

    def test_vendor_withdraw_blocked_when_approved_without_portal_kyc(self):
        """Approved vendors still need verified portal User.kyc_status to withdraw."""
        vu = User.objects.create_user(
            username="v_kyc_wd",
            password=self.pw,
            phone="9844444444",
            name="Vendor WD",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.PENDING,
        )
        vendor = Vendor.objects.create(
            user=vu,
            store_name="KYC WD Store",
            store_slug="kyc-wd-store",
            status=Vendor.Status.APPROVED,
        )
        ensure_vendor_wallet(vendor)
        w = vendor.wallet
        w.status = Wallet.Status.ACTIVE
        w.save(update_fields=["status"])
        wallet_service.credit_wallet(
            w,
            Decimal("5000"),
            wtype=WalletTransaction.Type.TOPUP,
            description="test vendor fund",
            performed_by=vu,
        )
        pa = PayoutAccount.objects.create(
            user=vu,
            type=PayoutAccount.Type.ESEWA,
            phone="9800000001",
        )
        tok, _ = Token.objects.get_or_create(user=vu)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.post(
            "/api/vendor/withdrawals/",
            {"amount": 100, "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "kyc_required")

    def test_vendor_withdraw_blocked_when_pending_vendor_and_kyc_required(self):
        vu = User.objects.create_user(
            username="v_kyc_pend",
            password=self.pw,
            phone="9833333333",
            name="Vendor Pend",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.PENDING,
        )
        vendor = Vendor.objects.create(
            user=vu,
            store_name="Pend Store",
            store_slug="pend-store-kyc",
            status=Vendor.Status.PENDING,
        )
        ensure_vendor_wallet(vendor)
        w = vendor.wallet
        w.status = Wallet.Status.ACTIVE
        w.save(update_fields=["status"])
        wallet_service.credit_wallet(
            w,
            Decimal("5000"),
            wtype=WalletTransaction.Type.TOPUP,
            description="test",
            performed_by=vu,
        )
        pa = PayoutAccount.objects.create(
            user=vu,
            type=PayoutAccount.Type.ESEWA,
            phone="9800000002",
        )
        tok, _ = Token.objects.get_or_create(user=vu)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.post(
            "/api/vendor/withdrawals/",
            {"amount": 100, "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "kyc_required")
