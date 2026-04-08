"""OTP signup referral resolution and deferred referred_by wallet bonus."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import OTPVerification, User, WalletBonus, WalletTransaction
from core.phone_auth import normalize_nepal_phone
from core.services.base import get_or_create_personal_wallet
from core.views.auth_otp import _resolve_signup_referrer


class OtpSignupReferralTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_otp_signup_with_referrer_id_credits_referrer(self):
        referrer = User.objects.create_user(
            username="ref_otp_a",
            password="x",
            phone="9810000101",
            name="Referrer OTP",
            role=User.Role.NORMAL,
        )
        get_or_create_personal_wallet(referrer)
        WalletBonus.objects.create(
            title="Ref rule",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("30.00"),
            is_percentage=False,
            min_topup=Decimal("0"),
            status=WalletBonus.Status.ACTIVE,
        )
        phone_new = "9810000102"
        self.client.post(
            "/api/auth/otp/send/",
            {"phone": phone_new, "purpose": "signup", "name": "Friend"},
            format="json",
        )
        otp = OTPVerification.objects.filter(phone=phone_new, purpose="signup").latest(
            "created_at"
        ).otp
        r = self.client.post(
            "/api/auth/otp/verify/",
            {
                "phone": phone_new,
                "otp": otp,
                "purpose": "signup",
                "name": "Friend",
                "portal": "portal",
                "referrer_id": referrer.pk,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        friend = User.objects.get(phone=phone_new)
        self.assertEqual(friend.referred_by_id, referrer.pk)
        ref_wallet = get_or_create_personal_wallet(referrer)
        ref_wallet.refresh_from_db()
        self.assertEqual(ref_wallet.balance, Decimal("30.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(
                reference_type="referral_wallet_bonus",
                reference_id=str(friend.pk),
            ).count(),
            1,
        )

    def test_otp_signup_with_referrer_kid(self):
        referrer = User.objects.create_user(
            username="ref_otp_b",
            password="x",
            phone="9810000103",
            name="Referrer KID",
            role=User.Role.NORMAL,
        )
        referrer.refresh_from_db()
        WalletBonus.objects.create(
            title="Ref kid",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("5.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        phone_new = "9810000104"
        self.client.post(
            "/api/auth/otp/send/",
            {"phone": phone_new, "purpose": "signup", "name": "F2"},
            format="json",
        )
        otp = OTPVerification.objects.filter(phone=phone_new, purpose="signup").latest(
            "created_at"
        ).otp
        r = self.client.post(
            "/api/auth/otp/verify/",
            {
                "phone": phone_new,
                "otp": otp,
                "purpose": "signup",
                "name": "F2",
                "referrer_kid": referrer.KID,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(User.objects.get(phone=phone_new).referred_by_id, referrer.pk)

    def test_otp_signup_invalid_referrer_returns_400(self):
        phone_new = "9810000105"
        self.client.post(
            "/api/auth/otp/send/",
            {"phone": phone_new, "purpose": "signup", "name": "X"},
            format="json",
        )
        otp = OTPVerification.objects.filter(phone=phone_new, purpose="signup").latest(
            "created_at"
        ).otp
        r = self.client.post(
            "/api/auth/otp/verify/",
            {
                "phone": phone_new,
                "otp": otp,
                "purpose": "signup",
                "name": "X",
                "referrer_id": 999999999,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(phone=phone_new).exists())

    def test_resolve_signup_referrer_rejects_same_phone(self):
        phone = "9810000106"
        referrer = User.objects.create_user(
            username="self_ref",
            password="x",
            phone=phone,
            name="Same",
            role=User.Role.NORMAL,
        )
        norm = normalize_nepal_phone(phone)
        self.assertIsNotNone(norm)
        ref, err = _resolve_signup_referrer({"referrer_id": referrer.pk}, norm)
        self.assertIsNone(ref)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)
        self.assertIn("yourself", (err.data.get("detail") or "").lower())

    def test_deferred_referred_by_triggers_bonus_once(self):
        referrer = User.objects.create_user(
            username="def_ref",
            password="x",
            phone="9810000108",
            name="Def Ref",
            role=User.Role.NORMAL,
        )
        get_or_create_personal_wallet(referrer)
        WalletBonus.objects.create(
            title="Def",
            type=WalletBonus.Type.REFERRAL,
            amount=Decimal("12.00"),
            status=WalletBonus.Status.ACTIVE,
        )
        friend = User.objects.create_user(
            username="def_fr",
            password="x",
            phone="9810000109",
            name="Friend Def",
            role=User.Role.NORMAL,
        )
        self.assertIsNone(friend.referred_by_id)
        with self.captureOnCommitCallbacks(execute=True):
            friend.referred_by = referrer
            friend.save(update_fields=["referred_by"])
        w = get_or_create_personal_wallet(referrer)
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("12.00"))
        with self.captureOnCommitCallbacks(execute=True):
            friend.save(update_fields=["name"])
        w.refresh_from_db()
        self.assertEqual(w.balance, Decimal("12.00"))
