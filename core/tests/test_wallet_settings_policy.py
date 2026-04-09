"""WalletSettings enforcement (limits, OTP, vendor toggle, checkout filtering)."""

from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import (
    EmployeeProfile,
    FamilyGroup,
    FamilyMember,
    OTPVerification,
    PayoutAccount,
    Role,
    User,
    Vendor,
    Wallet,
    WalletSettings,
    WalletTransaction,
)
from core.services import wallet_policy, wallet_service
from core.services.base import get_or_create_personal_wallet
from core.services.vendor_service import ensure_vendor_wallet
from core.tests.wallet_test_settings import relax_wallet_settings_for_tests


class WalletPolicyUnitTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.ws = WalletSettings.load()

    def test_compute_peer_transfer_fee_percentage(self):
        self.ws.transaction_fee_type = WalletSettings.FeeType.PERCENTAGE
        self.ws.transaction_fee_value = Decimal("2.00")
        self.ws.save(
            update_fields=["transaction_fee_type", "transaction_fee_value"]
        )
        self.assertEqual(
            wallet_policy.compute_peer_transfer_fee(Decimal("100.00")),
            Decimal("2.00"),
        )

    def test_daily_transfer_limit_blocks(self):
        u = User.objects.create_user(
            username="lim_u",
            password="x",
            phone="9810100101",
            name="L",
            role=User.Role.NORMAL,
        )
        u2 = User.objects.create_user(
            username="lim_u2",
            password="x",
            phone="9810100102",
            name="L2",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(u)
        w2 = get_or_create_personal_wallet(u2)
        w.balance = Decimal("10000")
        w.save(update_fields=["balance"])
        self.ws.daily_transfer_limit = Decimal("50")
        self.ws.save(update_fields=["daily_transfer_limit"])
        wallet_service.execute_transfer(
            w,
            w2,
            Decimal("30"),
            performed_by=u,
            reference_type="t",
            reference_id="a",
        )
        with self.assertRaises(ValueError):
            wallet_policy.assert_daily_transfer_limit(u, Decimal("25"))

    def test_validate_withdrawal_min(self):
        u = User.objects.create_user(
            username="wd_u",
            password="x",
            phone="9810100103",
            name="W",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(u)
        self.ws.min_withdrawal = Decimal("100")
        self.ws.save(update_fields=["min_withdrawal"])
        with self.assertRaises(ValueError):
            wallet_policy.validate_withdrawal_against_settings(w, Decimal("50"))
        wallet_policy.validate_withdrawal_against_settings(w, Decimal("100"))


class WalletPolicyAPITests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()
        self.pw = "TestWalletPol123!"

    def test_vendor_withdraw_403_when_vendor_wallet_disabled(self):
        ws = WalletSettings.load()
        ws.vendor_wallet_enabled = False
        ws.save(update_fields=["vendor_wallet_enabled"])
        vu = User.objects.create_user(
            username="v_wd_off",
            password=self.pw,
            phone="9810200201",
            name="V",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        vendor = Vendor.objects.create(
            user=vu,
            store_name="VOff",
            store_slug="v-off",
            status=Vendor.Status.APPROVED,
        )
        ensure_vendor_wallet(vendor)
        vw = vendor.wallet
        vw.status = Wallet.Status.ACTIVE
        vw.save(update_fields=["status"])
        wallet_service.credit_wallet(
            vw,
            Decimal("5000"),
            wtype=WalletTransaction.Type.TOPUP,
            description="t",
            performed_by=vu,
        )
        pa = PayoutAccount.objects.create(
            user=vu,
            type=PayoutAccount.Type.ESEWA,
            phone="9800000201",
        )
        tok, _ = Token.objects.get_or_create(user=vu)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.post(
            "/api/vendor/withdrawals/",
            {"amount": 200, "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.data.get("code"), "vendor_wallet_disabled")

    def test_peer_transfer_requires_otp_when_above_threshold(self):
        ws = WalletSettings.load()
        ws.otp_for_transfers_above = Decimal("50")
        ws.save(update_fields=["otp_for_transfers_above"])
        a = User.objects.create_user(
            username="pta",
            password=self.pw,
            phone="9810300301",
            name="A",
            role=User.Role.NORMAL,
        )
        b = User.objects.create_user(
            username="ptb",
            password=self.pw,
            phone="9810300302",
            name="B",
            role=User.Role.NORMAL,
        )
        g = FamilyGroup.objects.create(
            name="OTP fam",
            leader=a,
            status=FamilyGroup.Status.ACTIVE,
        )
        FamilyMember.objects.create(
            group=g,
            user=a,
            role=FamilyMember.Role.PARENT,
            status=FamilyMember.Status.ACTIVE,
        )
        FamilyMember.objects.create(
            group=g,
            user=b,
            role=FamilyMember.Role.PARENT,
            status=FamilyMember.Status.ACTIVE,
        )
        wa = get_or_create_personal_wallet(a)
        wb = get_or_create_personal_wallet(b)
        wa.balance = Decimal("10000")
        wa.save(update_fields=["balance"])
        tok, _ = Token.objects.get_or_create(user=a)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r0 = self.client.post(
            "/api/portal/wallet/transfer/",
            {"recipient": str(b.pk), "amount": "80"},
            format="json",
        )
        self.assertEqual(r0.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r0.data.get("code"), "otp_required")

        from core.services import otp_service

        row = otp_service.create_otp(a.phone, OTPVerification.Purpose.TRANSFER)
        r1 = self.client.post(
            "/api/portal/wallet/transfer/",
            {"recipient": str(b.pk), "amount": "80", "otp": row.otp},
            format="json",
        )
        self.assertEqual(r1.status_code, status.HTTP_200_OK, r1.data)
        wb.refresh_from_db()
        self.assertEqual(wb.balance, Decimal("80.00"))

    def test_checkout_wallet_list_respects_disabled_personal(self):
        ws = WalletSettings.load()
        ws.individual_wallet_enabled = False
        ws.save(update_fields=["individual_wallet_enabled"])
        parent = User.objects.create_user(
            username="chk_p",
            password=self.pw,
            phone="9810400401",
            name="P",
            role=User.Role.PARENT,
        )
        g = FamilyGroup.objects.create(
            name="ChkFam",
            leader=parent,
            status=FamilyGroup.Status.ACTIVE,
        )
        FamilyMember.objects.create(
            group=g,
            user=parent,
            role=FamilyMember.Role.PARENT,
            status=FamilyMember.Status.ACTIVE,
        )
        from core.services import family_portal_wallet_service

        family_portal_wallet_service.ensure_default_shared_wallet(g, parent)
        tok, _ = Token.objects.get_or_create(user=parent)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.get("/api/portal/orders/checkout-wallet/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        rows = r.data.get("payable_wallets") or []
        ids = {int(x["id"]) for x in rows}
        personal = get_or_create_personal_wallet(parent)
        self.assertNotIn(personal.pk, ids)


class WalletFreezeServiceTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()

    def test_debit_wallet_raises_when_frozen(self):
        u = User.objects.create_user(
            username="fz_d",
            password="x",
            phone="9810500501",
            name="F",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(u)
        w.balance = Decimal("100")
        w.status = Wallet.Status.FROZEN
        w.save(update_fields=["balance", "status"])
        with self.assertRaises(ValueError) as ctx:
            wallet_service.debit_wallet(
                w,
                Decimal("10"),
                wtype=WalletTransaction.Type.DEBIT,
                description="t",
                performed_by=u,
            )
        self.assertIn("frozen", str(ctx.exception).lower())

    def test_credit_wallet_raises_when_frozen_without_flag(self):
        u = User.objects.create_user(
            username="fz_c",
            password="x",
            phone="9810500502",
            name="C",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(u)
        w.status = Wallet.Status.FROZEN
        w.save(update_fields=["status"])
        with self.assertRaises(ValueError) as ctx:
            wallet_service.credit_wallet(
                w,
                Decimal("10"),
                wtype=WalletTransaction.Type.TOPUP,
                description="t",
                performed_by=u,
            )
        self.assertIn("frozen", str(ctx.exception).lower())

    def test_credit_wallet_refund_allows_frozen_target(self):
        u = User.objects.create_user(
            username="fz_r",
            password="x",
            phone="9810500503",
            name="R",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(u)
        w.status = Wallet.Status.FROZEN
        w.balance = Decimal("0")
        w.save(update_fields=["status", "balance"])
        wallet_service.credit_wallet(
            w,
            Decimal("25"),
            wtype=WalletTransaction.Type.REFUND_CREDIT,
            description="refund",
            performed_by=u,
            skip_max_balance=True,
            allow_frozen_target=True,
        )
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("25"))

    def test_execute_transfer_raises_when_sender_frozen(self):
        a = User.objects.create_user(
            username="fz_ta",
            password="x",
            phone="9810500504",
            name="A",
            role=User.Role.NORMAL,
        )
        b = User.objects.create_user(
            username="fz_tb",
            password="x",
            phone="9810500505",
            name="B",
            role=User.Role.NORMAL,
        )
        wa = get_or_create_personal_wallet(a)
        wb = get_or_create_personal_wallet(b)
        wa.balance = Decimal("100")
        wa.status = Wallet.Status.FROZEN
        wa.save(update_fields=["balance", "status"])
        with self.assertRaises(ValueError) as ctx:
            wallet_service.execute_transfer(
                wa,
                wb,
                Decimal("10"),
                performed_by=a,
                reference_type="t",
                reference_id="x",
            )
        self.assertIn("frozen", str(ctx.exception).lower())


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "wallet-freeze-admin-tests",
        }
    }
)
class WalletFreezeAdminAPITests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        from core.models import SecuritySettings

        ss = SecuritySettings.load()
        ss.rbac_enforced = True
        ss.otp_sensitive_crud = False
        ss.save()

        self.client = APIClient()
        self.pw = "WfAdm123!"
        self.super_admin = User.objects.create_user(
            username="wf_sa",
            password=self.pw,
            phone="9810600601",
            name="SA",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff_wm = User.objects.create_user(
            username="wf_st",
            password=self.pw,
            phone="9810600602",
            name="St",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        role = Role.objects.create(
            name="Wallet mod",
            permissions={"wallet-master": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff_wm,
            role=role,
            modules_access=["dashboard", "wallet-master"],
            status=EmployeeProfile.Status.ACTIVE,
        )
        owner = User.objects.create_user(
            username="wf_own",
            password=self.pw,
            phone="9810600603",
            name="Own",
            role=User.Role.NORMAL,
        )
        self.wallet = Wallet.objects.create(
            owner=owner,
            type=Wallet.Type.PERSONAL,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("0"),
        )

    def _token(self, user: User) -> str:
        tok, _ = Token.objects.get_or_create(user=user)
        return tok.key

    def test_staff_patch_wallet_status_forbidden(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self._token(self.staff_wm)}"
        )
        r = self.client.patch(
            f"/api/admin/wallets/{self.wallet.pk}/",
            {"status": "frozen"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.status, Wallet.Status.ACTIVE)

    def test_super_admin_patch_wallet_status_ok(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}"
        )
        r = self.client.patch(
            f"/api/admin/wallets/{self.wallet.pk}/",
            {"status": "frozen"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.status, Wallet.Status.FROZEN)

    def test_get_wallet_detail_ok_for_staff(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self._token(self.staff_wm)}"
        )
        r = self.client.get(f"/api/admin/wallets/{self.wallet.pk}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        self.assertEqual(r.data.get("id"), str(self.wallet.pk))
        self.assertEqual(r.data.get("status"), Wallet.Status.ACTIVE)

    def test_family_only_filter_on_wallets_list(self):
        g = FamilyGroup.objects.create(
            name="FamOnly",
            leader=self.super_admin,
            status=FamilyGroup.Status.ACTIVE,
        )
        w2 = Wallet.objects.create(
            owner=self.super_admin,
            type=Wallet.Type.PARENT,
            family_group=g,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("0"),
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self._token(self.staff_wm)}"
        )
        r = self.client.get("/api/admin/wallets/", {"family_only": "true", "page_size": 500})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        ids = {x["id"] for x in (r.data.get("results") or [])}
        self.assertIn(str(w2.pk), ids)
        self.assertNotIn(str(self.wallet.pk), ids)
