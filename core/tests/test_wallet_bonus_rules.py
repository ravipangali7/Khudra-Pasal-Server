"""WalletBonus rules: signup (existing), top-up, and referral."""

from decimal import Decimal

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
