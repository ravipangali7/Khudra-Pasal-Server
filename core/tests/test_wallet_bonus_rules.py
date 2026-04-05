"""WalletBonus rules: signup (existing), top-up, and referral."""

from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from core.models import PaymentTransaction, User, WalletBonus, WalletTransaction
from core.services import wallet_service
from core.services.base import get_or_create_personal_wallet


class WalletBonusRulesTests(TestCase):
    def test_topup_bonus_fixed_applied_once(self):
        user = User.objects.create_user(
            username="tbu1",
            password="x",
            phone="9800000001",
            name="Topup User",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        WalletBonus.objects.create(
            title="Topup extra",
            type=WalletBonus.Type.TOPUP,
            amount=Decimal("25.00"),
            is_percentage=False,
            min_topup=Decimal("100.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        pt = PaymentTransaction.objects.create(
            txn_ref="tx-topup-bonus-1",
            customer=user,
            amount=Decimal("200.00"),
            method=PaymentTransaction.Method.ESEWA,
            status=PaymentTransaction.Status.SUCCESS,
        )
        wt = wallet_service.credit_from_payment_transaction(pt)
        self.assertIsNotNone(wt)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("225.00"))
        bonus_txns = WalletTransaction.objects.filter(
            reference_type="topup_bonus",
            reference_id=str(pt.pk),
        )
        self.assertEqual(bonus_txns.count(), 1)
        self.assertEqual(bonus_txns.first().amount, Decimal("25.00"))

        # Idempotent: second call does not double top-up or bonus
        wt2 = wallet_service.credit_from_payment_transaction(pt)
        self.assertEqual(wt2, wt)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("225.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                reference_type="topup_bonus",
                reference_id=str(pt.pk),
            ).count(),
            1,
        )

    def test_topup_bonus_below_min_not_applied(self):
        user = User.objects.create_user(
            username="tbu2",
            password="x",
            phone="9800000002",
            name="Topup User 2",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        WalletBonus.objects.create(
            title="Big topup only",
            type=WalletBonus.Type.TOPUP,
            amount=Decimal("50.00"),
            min_topup=Decimal("500.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        pt = PaymentTransaction.objects.create(
            txn_ref="tx-topup-bonus-2",
            customer=user,
            amount=Decimal("100.00"),
            method=PaymentTransaction.Method.ESEWA,
            status=PaymentTransaction.Status.SUCCESS,
        )
        wallet_service.credit_from_payment_transaction(pt)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("100.00"))
        self.assertFalse(
            WalletTransaction.objects.filter(reference_type="topup_bonus").exists()
        )

    def test_topup_bonus_percentage(self):
        user = User.objects.create_user(
            username="tbu3",
            password="x",
            phone="9800000003",
            name="Topup User 3",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        WalletBonus.objects.create(
            title="10pct",
            type=WalletBonus.Type.TOPUP,
            amount=Decimal("10"),
            is_percentage=True,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        pt = PaymentTransaction.objects.create(
            txn_ref="tx-topup-bonus-3",
            customer=user,
            amount=Decimal("200.00"),
            method=PaymentTransaction.Method.ESEWA,
            status=PaymentTransaction.Status.SUCCESS,
        )
        wallet_service.credit_from_payment_transaction(pt)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("220.00"))

    def test_topup_bonus_after_credit_portal_parity(self):
        """Same TOPUP rules as gateway when crediting via portal-style flow."""
        user = User.objects.create_user(
            username="tbu4",
            password="x",
            phone="9800000004",
            name="Topup User 4",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        WalletBonus.objects.create(
            title="Topup extra portal",
            type=WalletBonus.Type.TOPUP,
            amount=Decimal("25.00"),
            is_percentage=False,
            min_topup=Decimal("100.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        with transaction.atomic():
            wt = wallet_service.credit_wallet(
                wallet,
                Decimal("200.00"),
                wtype=WalletTransaction.Type.TOPUP,
                description="Wallet top-up (esewa)",
                performed_by=user,
            )
            wallet_service.apply_topup_bonus_after_credit(
                wallet,
                Decimal("200.00"),
                bonus_reference_id=wt.txn_id,
                performed_by=user,
            )
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("225.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                reference_type="topup_bonus",
                reference_id=wt.txn_id,
            ).count(),
            1,
        )

    def test_topup_bonus_after_credit_idempotent(self):
        user = User.objects.create_user(
            username="tbu5",
            password="x",
            phone="9800000005",
            name="Topup User 5",
            role=User.Role.NORMAL,
        )
        wallet = get_or_create_personal_wallet(user)
        WalletBonus.objects.create(
            title="Bonus",
            type=WalletBonus.Type.TOPUP,
            amount=Decimal("5.00"),
            is_percentage=False,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        with transaction.atomic():
            wt = wallet_service.credit_wallet(
                wallet,
                Decimal("50.00"),
                wtype=WalletTransaction.Type.TOPUP,
                description="Top-up",
                performed_by=user,
            )
            wallet_service.apply_topup_bonus_after_credit(
                wallet,
                Decimal("50.00"),
                bonus_reference_id=wt.txn_id,
                performed_by=user,
            )
            wallet_service.apply_topup_bonus_after_credit(
                wallet,
                Decimal("50.00"),
                bonus_reference_id=wt.txn_id,
                performed_by=user,
            )
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("55.00"))

    def test_signup_bonus_best_rule_wins(self):
        WalletBonus.objects.create(
            title="Small welcome",
            type=WalletBonus.Type.SIGNUP,
            amount=Decimal("10.00"),
            is_percentage=False,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        WalletBonus.objects.create(
            title="Big welcome",
            type=WalletBonus.Type.SIGNUP,
            amount=Decimal("50.00"),
            is_percentage=False,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        user = User.objects.create_user(
            username="sigbest",
            password="x",
            phone="9800000020",
            name="Signup Best",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(user)
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("50.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                wallet=w,
                reference_type="signup_bonus",
            ).count(),
            1,
        )

    def test_signup_bonus_percentage_uses_min_topup_base(self):
        WalletBonus.objects.create(
            title="Pct welcome",
            type=WalletBonus.Type.SIGNUP,
            amount=Decimal("10"),
            is_percentage=True,
            min_topup=Decimal("1000.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        user = User.objects.create_user(
            username="sigpct",
            password="x",
            phone="9800000021",
            name="Signup Pct",
            role=User.Role.NORMAL,
        )
        w = get_or_create_personal_wallet(user)
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("100.00"))

    def test_referral_bonus_credits_referrer(self):
        referrer = User.objects.create_user(
            username="refsrc",
            password="x",
            phone="9800000010",
            name="Referrer",
            role=User.Role.NORMAL,
        )
        ref_wallet = get_or_create_personal_wallet(referrer)
        WalletBonus.objects.create(
            title="Refer friends",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("40.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        friend = User.objects.create_user(
            username="reffrd",
            password="x",
            phone="9800000011",
            name="Friend",
            role=User.Role.NORMAL,
            referred_by=referrer,
        )
        ref_wallet.refresh_from_db()
        self.assertEqual(ref_wallet.balance, Decimal("40.00"))
        self.assertTrue(
            WalletTransaction.objects.filter(
                reference_type="referral_wallet_bonus",
                reference_id=str(friend.pk),
            ).exists()
        )

    def test_referral_bonus_idempotent(self):
        referrer = User.objects.create_user(
            username="refsrc2",
            password="x",
            phone="9800000012",
            name="Referrer 2",
            role=User.Role.NORMAL,
        )
        WalletBonus.objects.create(
            title="Refer",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("10.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        friend = User.objects.create_user(
            username="reffrd2",
            password="x",
            phone="9800000013",
            name="Friend 2",
            role=User.Role.NORMAL,
            referred_by=referrer,
        )
        wallet_service.apply_referral_wallet_bonus(friend)
        wallet_service.apply_referral_wallet_bonus(friend)
        w = get_or_create_personal_wallet(referrer)
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("10.00"))

    def test_referral_bonus_best_rule_wins(self):
        referrer = User.objects.create_user(
            username="refbestsrc",
            password="x",
            phone="9800000014",
            name="Referrer Best",
            role=User.Role.NORMAL,
        )
        get_or_create_personal_wallet(referrer)
        WalletBonus.objects.create(
            title="Small ref",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("15.00"),
            is_percentage=False,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        WalletBonus.objects.create(
            title="Big ref",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("45.00"),
            is_percentage=False,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        User.objects.create_user(
            username="refbestfrd",
            password="x",
            phone="9800000015",
            name="Friend Best",
            role=User.Role.NORMAL,
            referred_by=referrer,
        )
        w = get_or_create_personal_wallet(referrer)
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("45.00"))

    def test_referral_bonus_percentage_uses_min_topup_base(self):
        referrer = User.objects.create_user(
            username="refpcts",
            password="x",
            phone="9800000016",
            name="Referrer Pct",
            role=User.Role.NORMAL,
        )
        get_or_create_personal_wallet(referrer)
        WalletBonus.objects.create(
            title="Ref pct",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("20"),
            is_percentage=True,
            min_topup=Decimal("200.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        friend = User.objects.create_user(
            username="refpctf",
            password="x",
            phone="9800000017",
            name="Friend Pct",
            role=User.Role.NORMAL,
            referred_by=referrer,
        )
        w = get_or_create_personal_wallet(referrer)
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("40.00"))
        self.assertTrue(
            WalletTransaction.objects.filter(
                reference_type="referral_wallet_bonus",
                reference_id=str(friend.pk),
            ).exists()
        )
