"""Customer family group create, invite, OTP, accept."""

import secrets
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    AutoApprovalRule,
    Category,
    FamilyGroup,
    FamilyGroupPermission,
    FamilyInvite,
    FamilyJoinRequest,
    FamilyMember,
    FamilyWalletCategory,
    Notification,
    OTPVerification,
    PayoutAccount,
    ProductRestriction,
    User,
    Wallet,
    WalletSettings,
    WalletTransaction,
    WalletWithdrawal,
)
from core.services import family_portal_wallet_service, family_service, otp_service, wallet_service
from core.services.family_service import get_platform_hub_group
from core.services.base import get_or_create_personal_wallet
from core.services.family_portal_wallet_service import (
    get_default_shared_wallet,
    get_member_family_wallet,
)
from rest_framework.authtoken.models import Token

from core.tests.wallet_test_settings import relax_wallet_settings_for_tests


class FamilyPortalFlowTests(TestCase):
    def _fund_family_master(self, amount: str):
        group = FamilyGroup.objects.get(leader=self.leader)
        family_portal_wallet_service.family_wallet_load(
            group=group,
            amount=Decimal(amount),
            performed_by=self.leader,
            category=None,
            method="test",
        )

    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.leader = User.objects.create_user(
            username="lead1",
            password=self.pw,
            phone="9811111111",
            name="Leader",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        self.child = User.objects.create_user(
            username="childu",
            password=self.pw,
            phone="9833333333",
            name="Kid",
            role=User.Role.NORMAL,
        )

    def test_create_group_invite_accept_child(self):
        r = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        token = r.data["token"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        r2 = self.client.post(
            "/api/portal/family/group/", {"name": "My Family"}, format="json"
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.leader.refresh_from_db()
        self.assertEqual(self.leader.role, User.Role.PARENT)

        r3 = self.client.post(
            "/api/portal/family/invites/",
            {
                "phone": self.child.phone,
                "role": "child",
                "spending_limit": "5000",
            },
            format="json",
        )
        self.assertEqual(r3.status_code, status.HTTP_201_CREATED)
        inv_token = r3.data["token"]

        self.client.credentials()
        r4 = self.client.post(
            "/api/auth/otp/send/",
            {
                "phone": self.child.phone,
                "purpose": "family_invite",
                "invite_token": inv_token,
            },
            format="json",
        )
        self.assertEqual(r4.status_code, status.HTTP_200_OK)
        otp = OTPVerification.objects.filter(
            phone=self.child.phone, purpose=OTPVerification.Purpose.FAMILY_INVITE
        ).latest("created_at").otp

        r5 = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.child.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r5.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {r5.data['token']}")

        r6 = self.client.post(
            "/api/portal/family/invites/accept/",
            {
                "token": inv_token,
                "otp": otp,
                "phone": self.child.phone,
            },
            format="json",
        )
        self.assertEqual(r6.status_code, status.HTTP_200_OK)
        self.assertTrue(r6.data.get("pending_approval"))
        self.assertIn("join_request", r6.data)
        inv = FamilyInvite.objects.get(token=inv_token)
        self.assertEqual(inv.status, FamilyInvite.Status.PENDING)
        self.child.refresh_from_db()
        self.assertEqual(self.child.role, User.Role.NORMAL)

        jr_id = r6.data["join_request"]["id"]
        self.client.credentials()
        r7 = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r7.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {r7.data['token']}")
        r8 = self.client.patch(
            f"/api/portal/family/join-request/{jr_id}/",
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(r8.status_code, status.HTTP_200_OK)
        self.child.refresh_from_db()
        self.assertEqual(self.child.role, User.Role.CHILD)
        self.assertTrue(
            FamilyMember.objects.filter(
                group__leader=self.leader, user=self.child
            ).exists()
        )

    def test_website_family_invite_meta(self):
        from core.models import FamilyInvite
        from django.utils import timezone
        from datetime import timedelta

        group = FamilyGroup.objects.create(
            name="G",
            leader=self.leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        inv = FamilyInvite.objects.create(
            group=group,
            invited_by=self.leader,
            invite_method=FamilyInvite.InviteMethod.PHONE,
            phone=self.child.phone,
            token="a" * 64,
            role=FamilyInvite.Role.CHILD,
            expires_at=timezone.now() + timedelta(days=7),
            status=FamilyInvite.Status.PENDING,
        )
        r = self.client.get(f"/api/website/family-invite/{inv.token}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["group_name"], "G")
        self.assertEqual(r.data["role"], "child")

    def test_switch_portal_context_can_create_for_normal_without_family(self):
        login = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r = self.client.get("/api/portal/switch-portal/context/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["has_family_portal_access"], False)
        self.assertEqual(r.data["has_child_portal_access"], False)
        self.assertEqual(r.data["can_create_family_group"], True)
        self.assertGreaterEqual(len(r.data["family_group_types"]), 1)

    def test_switch_portal_context_disables_create_for_active_member(self):
        FamilyGroup.objects.create(
            name="Fam",
            leader=self.leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        FamilyMember.objects.create(
            group=FamilyGroup.objects.get(name="Fam"),
            user=self.child,
            role=FamilyMember.Role.SPOUSE,
            status=FamilyMember.Status.ACTIVE,
        )
        login = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.child.phone, "password": self.pw},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r = self.client.get("/api/portal/switch-portal/context/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["has_family_portal_access"], True)
        self.assertEqual(r.data["can_create_family_group"], False)

    def _login_leader_with_family(self):
        login = self.client.post(
            "/api/portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r = self.client.post(
            "/api/portal/family/group/", {"name": "Wallet Fam"}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)

    def test_post_family_members_provisions_member_and_overview_fields(self):
        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/members/",
            {
                "name": "New Kid",
                "email": "kid@test.com",
                "phone": "9844444444",
                "role": "child",
                "age": 10,
                "invite_method": "link",
                "spending_limit": "3000",
                "initial_balance": "0",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertTrue(r.data.get("ok"))
        self.assertIn("member", r.data)
        self.assertEqual(r.data["member"]["phone"], "9844444444")
        self.assertEqual(r.data["member"]["role"], "child")
        self.assertIn("group", r.data["member"])
        new_user = User.objects.filter(phone="9844444444").first()
        self.assertIsNotNone(new_user)
        self.assertEqual(new_user.role, User.Role.CHILD)
        self.assertTrue(
            FamilyMember.objects.filter(
                user=new_user,
                group__leader=self.leader,
                status=FamilyMember.Status.ACTIVE,
            ).exists()
        )

        ov = self.client.get("/api/portal/family/members/")
        self.assertEqual(ov.status_code, status.HTTP_200_OK)
        self.assertIn("master_wallet_balance", ov.data)
        self.assertIn("join_requests", ov.data)
        self.assertIn("add_member_roles", ov.data)
        self.assertGreaterEqual(len(ov.data["add_member_roles"]), 1)
        member_phones = {m["phone"] for m in ov.data["members"]}
        self.assertIn("9844444444", member_phones)
        for m in ov.data["members"]:
            if m["phone"] == "9844444444":
                self.assertIn("group", m)
                self.assertEqual(m["group"]["name"], ov.data["group"]["name"])

    def test_family_overview_member_balance_matches_portal_me_when_personal_split_from_parent(
        self,
    ):
        """Unscoped PERSONAL funds + empty family PARENT wallet: overview balance matches /portal/me/."""
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        parent_w = Wallet.objects.filter(
            owner=self.leader,
            family_group=group,
            type=Wallet.Type.PARENT,
            status=Wallet.Status.ACTIVE,
        ).first()
        self.assertIsNotNone(parent_w)
        parent_w.balance = Decimal("0.00")
        parent_w.save(update_fields=["balance", "updated_at"])
        Wallet.objects.create(
            owner=self.leader,
            type=Wallet.Type.PERSONAL,
            label="Unscoped",
            balance=Decimal("1500.00"),
            status=Wallet.Status.ACTIVE,
            family_group=None,
        )
        me = self.client.get("/api/portal/me/")
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(float(me.data["wallet_balance"]), 1500.0)

        ov = self.client.get("/api/portal/family/members/")
        self.assertEqual(ov.status_code, status.HTTP_200_OK)
        leader_row = next(m for m in ov.data["members"] if m["phone"] == self.leader.phone)
        self.assertEqual(float(leader_row["balance"]), 1500.0)

    def test_family_wallet_load_and_category(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        shared = get_default_shared_wallet(group)
        self.assertIsNotNone(shared)
        self.assertEqual(shared.balance, Decimal("0.00"))

        r = self.client.post(
            "/api/portal/family/wallet/load/",
            {"amount": "150.50", "method": "esewa"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data.get("flow"), "esewa_redirect")
        shared.refresh_from_db()
        self.assertEqual(shared.balance, Decimal("0.00"))
        self._fund_family_master("150.50")
        shared.refresh_from_db()
        self.assertEqual(shared.balance, Decimal("150.50"))

        r_cat = self.client.post(
            "/api/portal/family/wallet/categories/",
            {"name": "School"},
            format="json",
        )
        self.assertEqual(r_cat.status_code, status.HTTP_201_CREATED)
        cat = FamilyWalletCategory.objects.get(group=group, name="School")
        w2 = Wallet.objects.get(family_group=group, family_category=cat)
        self.assertEqual(w2.balance, Decimal("0.00"))

        bad = self.client.post(
            "/api/portal/family/wallet/load/",
            {"amount": "10", "method": "esewa", "category_id": cat.pk},
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

    def test_family_wallet_load_creates_shared_wallet_if_missing(self):
        """Legacy or incomplete groups may lack the main SHARED pool; load should create it."""
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        shared = get_default_shared_wallet(group)
        self.assertIsNotNone(shared)
        old_pk = shared.pk
        shared.delete()
        self.assertIsNone(get_default_shared_wallet(group))

        r = self.client.post(
            "/api/portal/family/wallet/load/",
            {"amount": "25", "method": "esewa"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data.get("flow"), "esewa_redirect")
        new_shared = get_default_shared_wallet(group)
        self.assertIsNotNone(new_shared)
        self.assertNotEqual(new_shared.pk, old_pk)
        self.assertEqual(new_shared.balance, Decimal("0.00"))
        self._fund_family_master("25")
        new_shared.refresh_from_db()
        self.assertEqual(new_shared.balance, Decimal("25.00"))

    def test_approve_join_request_adds_existing_user(self):
        self._login_leader_with_family()
        spouse = User.objects.create_user(
            username="spouse1",
            password=self.pw,
            phone="9822222222",
            name="Spouse",
            role=User.Role.NORMAL,
        )
        group = FamilyGroup.objects.get(leader=self.leader)
        token = secrets.token_hex(32)
        inv = FamilyInvite.objects.create(
            group=group,
            invited_by=self.leader,
            invite_method=FamilyInvite.InviteMethod.PHONE,
            phone=spouse.phone,
            token=token,
            role=FamilyInvite.Role.SPOUSE,
            expires_at=timezone.now() + timedelta(days=7),
            status=FamilyInvite.Status.PENDING,
        )
        jr = FamilyJoinRequest.objects.create(
            group=group,
            requested_by=self.leader,
            name=spouse.name,
            phone=spouse.phone,
            role=FamilyJoinRequest.Role.SPOUSE,
            status=FamilyJoinRequest.Status.PENDING,
            invite=inv,
        )
        r2 = self.client.patch(
            f"/api/portal/family/join-request/{jr.id}/",
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.data["status"], FamilyJoinRequest.Status.APPROVED)
        self.assertTrue(
            FamilyMember.objects.filter(
                group__leader=self.leader, user=spouse
            ).exists()
        )

    def test_reject_join_request_expires_invite(self):
        from core.models import FamilyInvite

        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/join-request/",
            {
                "name": "X",
                "phone": "9877777777",
                "role": "child",
                "invite_method": "phone",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        jr_id = r.data["join_request"]["id"]
        inv = FamilyInvite.objects.get(token=r.data["invite"]["token"])
        r2 = self.client.patch(
            f"/api/portal/family/join-request/{jr_id}/",
            {"action": "reject"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        inv.refresh_from_db()
        self.assertEqual(inv.status, FamilyInvite.Status.EXPIRED)

    @patch("core.services.family_join_request_service.otp_service.send_template_sms")
    def test_reject_join_request_sends_sms(self, mock_sms):
        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/join-request/",
            {
                "name": "X",
                "phone": "9877777777",
                "role": "child",
                "invite_method": "phone",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        jr_id = r.data["join_request"]["id"]
        r2 = self.client.patch(
            f"/api/portal/family/join-request/{jr_id}/",
            {"action": "reject"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        mock_sms.assert_called_once()
        args, _kw = mock_sms.call_args
        self.assertEqual(args[0], "9877777777")
        self.assertIn("not approved", args[1].lower())

    def _customer_auth_header(self, user: User) -> str:
        tok, _ = Token.objects.get_or_create(user=user)
        return f"Token {tok.key}"

    def test_share_link_post_requires_authentication(self):
        self._login_leader_with_family()
        r = self.client.post("/api/portal/family/join-share-link/", {}, format="json")
        link_token = r.data["token"]
        self.client.credentials()
        p = self.client.post(
            f"/api/website/family-portal-join/{link_token}/",
            {"name": "X", "phone": "9812345678"},
            format="json",
        )
        self.assertEqual(p.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_share_link_post_phone_mismatch_returns_400(self):
        self._login_leader_with_family()
        r = self.client.post("/api/portal/family/join-share-link/", {}, format="json")
        link_token = r.data["token"]
        self.client.credentials()
        applicant = User.objects.create_user(
            username="applicant_mis",
            password=self.pw,
            phone="9855555555",
            name="Applicant",
            role=User.Role.NORMAL,
        )
        self.client.credentials(HTTP_AUTHORIZATION=self._customer_auth_header(applicant))
        p = self.client.post(
            f"/api/website/family-portal-join/{link_token}/",
            {"name": "Applicant", "phone": self.leader.phone},
            format="json",
        )
        self.assertEqual(p.status_code, status.HTTP_400_BAD_REQUEST)

    def test_share_link_public_get_post_and_approve(self):
        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/join-share-link/",
            {"title": "Join us", "default_role": "child"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        token = r.data["token"]
        self.client.credentials()

        g = self.client.get(f"/api/website/family-portal-join/{token}/")
        self.assertEqual(g.status_code, status.HTTP_200_OK)
        self.assertEqual(g.data["title"], "Join us")
        self.assertTrue(g.data["ok"])

        applicant = User.objects.create_user(
            username="applicant985",
            password=self.pw,
            phone="9855555555",
            name="Applicant",
            role=User.Role.NORMAL,
        )
        self.client.credentials(HTTP_AUTHORIZATION=self._customer_auth_header(applicant))
        p = self.client.post(
            f"/api/website/family-portal-join/{token}/",
            {
                "name": "Applicant",
                "email": "a@example.com",
                "phone": "9855555555",
            },
            format="json",
        )
        self.assertEqual(p.status_code, status.HTTP_201_CREATED)
        jr = FamilyJoinRequest.objects.get(phone="9855555555")
        self.assertEqual(jr.source, FamilyJoinRequest.Source.SHARE_LINK)
        self.assertEqual(jr.requested_by_id, applicant.pk)
        self.assertIsNotNone(jr.join_link_id)

        login2 = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login2.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login2.data['token']}")
        ap = self.client.patch(
            f"/api/portal/family/join-request/{jr.id}/",
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(ap.status_code, status.HTTP_200_OK)
        u = User.objects.filter(phone="9855555555").first()
        self.assertIsNotNone(u)
        self.assertTrue(
            FamilyMember.objects.filter(
                group__leader=self.leader, user=u, status=FamilyMember.Status.ACTIVE
            ).exists()
        )

    @patch("core.services.family_join_request_service.otp_service.send_template_sms")
    def test_share_link_reject_sends_sms(self, mock_sms):
        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/join-share-link/",
            {},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        token = r.data["token"]
        self.client.credentials()
        applicant = User.objects.create_user(
            username="applicant984",
            password=self.pw,
            phone="9844444444",
            name="R",
            role=User.Role.NORMAL,
        )
        self.client.credentials(HTTP_AUTHORIZATION=self._customer_auth_header(applicant))
        self.client.post(
            f"/api/website/family-portal-join/{token}/",
            {"name": "R", "phone": "9844444444"},
            format="json",
        )
        jr = FamilyJoinRequest.objects.get(phone="9844444444")
        login2 = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login2.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login2.data['token']}")
        self.client.patch(
            f"/api/portal/family/join-request/{jr.id}/",
            {"action": "reject"},
            format="json",
        )
        mock_sms.assert_called_once()
        self.assertEqual(mock_sms.call_args[0][0], "9844444444")

    def test_share_link_duplicate_phone_returns_409(self):
        self._login_leader_with_family()
        r = self.client.post("/api/portal/family/join-share-link/", {}, format="json")
        token = r.data["token"]
        self.client.credentials()
        applicant = User.objects.create_user(
            username="applicant982",
            password=self.pw,
            phone="9822222222",
            name="A",
            role=User.Role.NORMAL,
        )
        auth = self._customer_auth_header(applicant)
        self.client.credentials(HTTP_AUTHORIZATION=auth)
        self.client.post(
            f"/api/website/family-portal-join/{token}/",
            {"name": "A", "phone": "9822222222"},
            format="json",
        )
        p2 = self.client.post(
            f"/api/website/family-portal-join/{token}/",
            {"name": "B", "phone": "9822222222"},
            format="json",
        )
        self.assertEqual(p2.status_code, status.HTTP_409_CONFLICT)

    def test_join_share_link_forbidden_for_child_only_member(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        self.child.role = User.Role.CHILD
        self.child.save(update_fields=["role"])
        ch_login = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": self.child.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(ch_login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {ch_login.data['token']}")
        r = self.client.post("/api/portal/family/join-share-link/", {}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_post_family_members_honors_member_role(self):
        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/members/",
            {
                "name": "Partner",
                "phone": "9898989898",
                "role": "spouse",
                "invite_method": "phone",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data["member"]["role"], "spouse")
        u = User.objects.get(phone="9898989898")
        self.assertEqual(u.role, User.Role.NORMAL)
        fm = FamilyMember.objects.get(user=u, group__leader=self.leader)
        self.assertEqual(fm.role, FamilyMember.Role.SPOUSE)

    def test_post_family_members_batch(self):
        self._login_leader_with_family()
        r = self.client.post(
            "/api/portal/family/members/batch/",
            {
                "members": [
                    {"name": "Kid A", "phone": "9841111111", "role": "child"},
                    {"name": "CoParent", "phone": "9841111112", "role": "spouse"},
                ],
                "invite_method": "phone",
                "spending_limit": "100",
                "initial_balance": "0",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(r.data["results"]), 2)
        for item in r.data["results"]:
            self.assertTrue(item.get("ok"))
            self.assertIn("member", item)
            self.assertIn("group", item["member"])
        self.assertTrue(User.objects.filter(phone="9841111111", role=User.Role.CHILD).exists())
        self.assertTrue(
            User.objects.filter(phone="9841111112", role=User.Role.NORMAL).exists()
        )

    def test_family_product_restrictions_crud(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        cat = Category.objects.create(
            name="RestrictCat", slug=f"rcat-{secrets.token_hex(4)}"
        )
        r = self.client.get("/api/portal/family/product-restrictions/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["results"], [])

        r2 = self.client.patch(
            "/api/portal/family/product-restrictions/",
            {"category_id": cat.pk, "is_blocked": True},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertTrue(
            ProductRestriction.objects.filter(
                group=group, category=cat, family_member__isnull=True
            ).exists()
        )

        r3 = self.client.get("/api/portal/family/product-restrictions/")
        self.assertEqual(r3.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r3.data["results"]), 1)
        self.assertTrue(r3.data["results"][0]["is_blocked"])

        r4 = self.client.put(
            "/api/portal/family/product-restrictions/",
            {
                "rules": [
                    {
                        "category_id": cat.pk,
                        "is_blocked": False,
                        "requires_approval": True,
                        "max_price": None,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(r4.status_code, status.HTTP_200_OK)
        row = ProductRestriction.objects.get(group=group, category=cat)
        self.assertFalse(row.is_blocked)
        self.assertTrue(row.requires_approval)

        other = User.objects.create_user(
            username="lead2pr",
            password=self.pw,
            phone="9855555555",
            name="OtherLead",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        self.client.credentials()
        login2 = self.client.post(
            "/api/portal/auth/login/",
            {"phone": other.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login2.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login2.data['token']}")
        r5 = self.client.post(
            "/api/portal/family/group/", {"name": "Other Fam"}, format="json"
        )
        self.assertEqual(r5.status_code, status.HTTP_201_CREATED)
        r6 = self.client.get("/api/portal/family/product-restrictions/")
        self.assertEqual(r6.status_code, status.HTTP_200_OK)
        self.assertEqual(r6.data["results"], [])

    def test_patch_family_member_role_updates_user_for_child(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        fm = FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        User.objects.filter(pk=self.child.pk).update(role=User.Role.CHILD)
        r = self.client.patch(
            f"/api/portal/family/members/{fm.pk}/",
            {"role": "spouse"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data["role"], "spouse")
        self.child.refresh_from_db()
        self.assertEqual(self.child.role, User.Role.NORMAL)

        leader_fm = FamilyMember.objects.get(group=group, user=self.leader)
        r2 = self.client.patch(
            f"/api/portal/family/members/{leader_fm.pk}/",
            {"role": "child"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("leader", (r2.data.get("detail") or "").lower())

    def test_join_flat_uses_platform_hub(self):
        if not get_platform_hub_group(FamilyGroup.Type.FLAT):
            hub_leader = User.objects.create_user(
                username="flat_hub_seed",
                password=self.pw,
                phone="9755555555",
                name="Flat hub",
                role=User.Role.NORMAL,
            )
            FamilyGroup.objects.create(
                name="Platform FLAT hub",
                leader=hub_leader,
                type=FamilyGroup.Type.FLAT,
                status=FamilyGroup.Status.ACTIVE,
                is_platform_hub=True,
            )
        other = User.objects.create_user(
            username="flatjoin",
            password=self.pw,
            phone="9777777777",
            name="FlatUser",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        login = self.client.post(
            "/api/portal/auth/login/",
            {"phone": other.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r = self.client.post(
            "/api/portal/family/group/",
            {"name": "ignored", "type": FamilyGroup.Type.FLAT},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        other.refresh_from_db()
        self.assertEqual(other.role, User.Role.PARENT)
        g = FamilyGroup.objects.get(pk=int(r.data["id"]))
        self.assertTrue(g.is_platform_hub)
        self.assertEqual(g.type, FamilyGroup.Type.FLAT)

    def test_distribute_records_category_on_transactions(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        self.client.post(
            "/api/portal/family/wallet/categories/",
            {"name": "Food", "allowed_member_roles": ["child"]},
            format="json",
        )
        cat = FamilyWalletCategory.objects.get(group=group, name="Food")
        self._fund_family_master("200")
        master = get_default_shared_wallet(group)
        w_cat = Wallet.objects.get(
            family_group=group, family_category=cat, type=Wallet.Type.SHARED
        )
        tr = self.client.post(
            "/api/portal/family/wallet/transfer/",
            {
                "from_wallet_id": str(master.pk),
                "to_wallet_id": str(w_cat.pk),
                "amount": "200",
            },
            format="json",
        )
        self.assertEqual(tr.status_code, status.HTTP_200_OK)
        child_user = User.objects.create_user(
            username="distchild",
            password=self.pw,
            phone="9766666666",
            name="DistChild",
            role=User.Role.CHILD,
        )
        fm_child = FamilyMember.objects.create(
            group=group,
            user=child_user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        Wallet.objects.create(
            owner=child_user,
            type=Wallet.Type.CHILD,
            label="Child wallet",
            family_group=group,
            status=Wallet.Status.ACTIVE,
        )
        r = self.client.post(
            "/api/portal/family/wallet/distribute/",
            {
                "member_id": fm_child.pk,
                "amount": "50",
                "category_id": cat.pk,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        pair = WalletTransaction.objects.filter(
            reference_type="family_distribute",
            family_wallet_category=cat,
        )
        self.assertEqual(pair.count(), 2)

    def test_child_peer_transfer_respects_permission_flag(self):
        leader = self.leader
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=leader)
        perm, _ = FamilyGroupPermission.objects.get_or_create(group=group)
        perm.allow_peer_transfers = True
        perm.save()
        c1 = User.objects.create_user(
            username="c1",
            password=self.pw,
            phone="9755555555",
            name="C1",
            role=User.Role.CHILD,
        )
        c2 = User.objects.create_user(
            username="c2",
            password=self.pw,
            phone="9744444444",
            name="C2",
            role=User.Role.CHILD,
        )
        fm1 = FamilyMember.objects.create(
            group=group,
            user=c1,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        fm2 = FamilyMember.objects.create(
            group=group,
            user=c2,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        w1 = Wallet.objects.create(
            owner=c1,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("30.00"),
        )
        Wallet.objects.create(
            owner=c2,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
        )
        self.client.credentials()
        login_c1 = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": c1.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login_c1.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login_c1.data['token']}")
        ok = self.client.post(
            "/api/portal/child/wallet/peer-transfer/",
            {"to_member_id": fm2.pk, "amount": "10"},
            format="json",
        )
        self.assertEqual(ok.status_code, status.HTTP_200_OK)
        w1.refresh_from_db()
        self.assertEqual(w1.balance, Decimal("20.00"))

        perm.allow_peer_transfers = False
        perm.save()
        bad = self.client.post(
            "/api/portal/child/wallet/peer-transfer/",
            {"to_member_id": fm2.pk, "amount": "5"},
            format="json",
        )
        self.assertEqual(bad.status_code, status.HTTP_403_FORBIDDEN)

    def test_self_profile_group_matches_overview_when_multiple_memberships(self):
        """Self-profile must use same canonical group as family overview (not lowest FamilyMember pk)."""
        hub_leader = User.objects.create_user(
            username="hubleader",
            password=self.pw,
            phone="9888888888",
            name="Hub Leader",
            role=User.Role.NORMAL,
        )
        hub = FamilyGroup.objects.create(
            name="Platform Hub",
            leader=hub_leader,
            type=FamilyGroup.Type.FLAT,
            is_platform_hub=True,
            status=FamilyGroup.Status.ACTIVE,
        )
        FamilyGroupPermission.objects.get_or_create(group=hub)
        FamilyMember.objects.create(
            group=hub,
            user=self.leader,
            role=FamilyMember.Role.MANAGER,
            status=FamilyMember.Status.ACTIVE,
        )
        private = FamilyGroup.objects.create(
            name="Private Fam",
            leader=self.leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        FamilyGroupPermission.objects.get_or_create(group=private)
        FamilyMember.objects.create(
            group=private,
            user=self.leader,
            role=FamilyMember.Role.PARENT,
            status=FamilyMember.Status.ACTIVE,
        )
        User.objects.filter(pk=self.leader.pk).update(role=User.Role.PARENT)
        fm_hub = FamilyMember.objects.get(user=self.leader, group=hub)
        fm_private = FamilyMember.objects.get(user=self.leader, group=private)
        self.assertLess(fm_hub.pk, fm_private.pk)

        login = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": self.leader.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")

        ov = self.client.get("/api/portal/family/members/")
        self.assertEqual(ov.status_code, status.HTTP_200_OK)
        prof = self.client.get("/api/portal/self-profile/")
        self.assertEqual(prof.status_code, status.HTTP_200_OK)

        self.assertEqual(ov.data["group"]["id"], prof.data["family_group_id"])
        self.assertEqual(ov.data["group"]["name"], prof.data["family_group_name"])
        self.assertEqual(ov.data["group"]["name"], "Private Fam")

    def test_family_overview_includes_viewer_and_leader_id(self):
        self._login_leader_with_family()
        ov = self.client.get("/api/portal/family/members/")
        self.assertEqual(ov.status_code, status.HTTP_200_OK)
        self.assertIn("viewer", ov.data)
        self.assertEqual(str(self.leader.pk), ov.data["viewer"]["user_id"])
        self.assertTrue(ov.data["viewer"]["is_leader"])
        self.assertEqual(ov.data["group"]["leader_id"], str(self.leader.pk))

    def test_patch_member_spending_limits_and_freeze_syncs_wallets(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        fm = FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        User.objects.filter(pk=self.child.pk).update(role=User.Role.CHILD)
        w = Wallet.objects.create(
            owner=self.child,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
        )
        r = self.client.patch(
            f"/api/portal/family/members/{fm.pk}/",
            {
                "spending_limit_daily": "100",
                "spending_limit_weekly": "500",
                "spending_limit_monthly": "2000",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        fm.refresh_from_db()
        self.assertEqual(fm.spending_limit_daily, Decimal("100"))
        r2 = self.client.patch(
            f"/api/portal/family/members/{fm.pk}/",
            {"status": "frozen"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        w.refresh_from_db()
        self.assertEqual(w.status, Wallet.Status.FROZEN)
        r3 = self.client.patch(
            f"/api/portal/family/members/{fm.pk}/",
            {"status": "active"},
            format="json",
        )
        self.assertEqual(r3.status_code, status.HTTP_200_OK)
        w.refresh_from_db()
        self.assertEqual(w.status, Wallet.Status.ACTIVE)

    def test_patch_member_spending_limits_rejects_inconsistent_order(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        fm = FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        r = self.client.patch(
            f"/api/portal/family/members/{fm.pk}/",
            {
                "spending_limit_daily": "500",
                "spending_limit_weekly": "100",
                "spending_limit_monthly": "2000",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        err_blob = str(r.data).lower()
        self.assertIn("spending limits", err_blob)

    def test_delete_family_member_removes_row(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        fm = FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.SPOUSE,
            status=FamilyMember.Status.ACTIVE,
        )
        r = self.client.delete(f"/api/portal/family/members/{fm.pk}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertFalse(FamilyMember.objects.filter(pk=fm.pk).exists())

    def test_delete_leader_member_rejected(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        leader_fm = FamilyMember.objects.get(group=group, user=self.leader)
        r = self.client.delete(f"/api/portal/family/members/{leader_fm.pk}/")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_auto_approval_rules_crud(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        cat = Category.objects.filter(status=Category.Status.ACTIVE).first()
        if not cat:
            cat = Category.objects.create(
                name="TestCatAP",
                slug=f"test-ap-{secrets.token_hex(4)}",
                status=Category.Status.ACTIVE,
            )
        r = self.client.post(
            "/api/portal/family/auto-approval-rules/",
            {
                "name": "Snacks",
                "description": "",
                "category": cat.pk,
                "max_amount": "500.00",
                "is_enabled": True,
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        rid = r.data["id"]
        self.assertTrue(AutoApprovalRule.objects.filter(pk=rid, group=group).exists())
        r2 = self.client.get("/api/portal/family/auto-approval-rules/")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(len(r2.data["results"]), 1)
        r3 = self.client.patch(
            f"/api/portal/family/auto-approval-rules/{rid}/",
            {"is_enabled": False, "name": "Snacks2"},
            format="json",
        )
        self.assertEqual(r3.status_code, status.HTTP_200_OK)
        self.assertEqual(r3.data["name"], "Snacks2")
        self.assertFalse(r3.data["is_enabled"])
        r4 = self.client.delete(f"/api/portal/family/auto-approval-rules/{rid}/")
        self.assertEqual(r4.status_code, status.HTTP_200_OK)
        self.assertFalse(AutoApprovalRule.objects.filter(pk=rid).exists())

    def test_family_wallet_transactions_signed_amount_and_flow(self):
        self._login_leader_with_family()
        self._fund_family_master("100")
        r = self.client.get("/api/portal/family/wallet-transactions/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(r.data["results"]), 1)
        row = r.data["results"][0]
        self.assertEqual(row["flow"], "in")
        self.assertGreater(row["signed_amount"], 0)
        self.assertEqual(row["status"], "completed")

    def test_child_portal_summary_and_transactions_parent_distribute(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        self._fund_family_master("300")
        master = get_default_shared_wallet(group)
        child_user = User.objects.create_user(
            username="childsum",
            password=self.pw,
            phone="9722222222",
            name="ChildSum",
            role=User.Role.CHILD,
        )
        fm_child = FamilyMember.objects.create(
            group=group,
            user=child_user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        Wallet.objects.create(
            owner=child_user,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("0.00"),
        )
        Wallet.objects.create(
            owner=child_user,
            type=Wallet.Type.PERSONAL,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("1500.00"),
        )
        self.client.post(
            "/api/portal/family/wallet/distribute/",
            {"member_id": fm_child.pk, "amount": "40"},
            format="json",
        )
        master.refresh_from_db()
        self.client.credentials()
        login = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": child_user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r_sum = self.client.get("/api/portal/child/summary/")
        self.assertEqual(r_sum.status_code, status.HTTP_200_OK)
        self.assertAlmostEqual(r_sum.data["parentLoaded"], float(master.balance), places=2)
        self.assertEqual(r_sum.data["selfLoaded"], 40.0)
        self.assertEqual(r_sum.data["personalBalance"], 1500.0)
        self.assertAlmostEqual(
            r_sum.data["totalBalance"],
            float(master.balance) + 40.0 + 1500.0,
            places=2,
        )
        r_tx = self.client.get("/api/portal/child/wallet-transactions/")
        self.assertEqual(r_tx.status_code, status.HTTP_200_OK)
        inc = [x for x in r_tx.data["results"] if x.get("reference_type") == "family_distribute"]
        self.assertTrue(inc)
        self.assertEqual(inc[0]["type"], "parent")
        self.assertEqual(inc[0]["wallet"], "Parent")

    def test_child_wallet_topup_and_withdraw(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        perm, _ = FamilyGroupPermission.objects.get_or_create(group=group)
        perm.allow_cash_withdrawal = True
        perm.save()
        child_user = User.objects.create_user(
            username="childtw",
            password=self.pw,
            phone="9711111111",
            name="ChildTW",
            role=User.Role.CHILD,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        FamilyMember.objects.create(
            group=group,
            user=child_user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        cw = Wallet.objects.create(
            owner=child_user,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("10.00"),
        )
        self.client.credentials()
        login = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": child_user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r_top = self.client.post(
            "/api/portal/child/wallet/topup/",
            {"amount": "25", "method": "esewa"},
            format="json",
        )
        self.assertEqual(r_top.status_code, status.HTTP_200_OK)
        self.assertEqual(r_top.data.get("flow"), "esewa_redirect")
        cw.refresh_from_db()
        self.assertEqual(cw.balance, Decimal("10.00"))
        wallet_service.credit_wallet(
            cw,
            Decimal("25"),
            wtype=WalletTransaction.Type.TOPUP,
            description="Test top-up credit",
            performed_by=child_user,
        )
        cw.refresh_from_db()
        self.assertEqual(cw.balance, Decimal("35.00"))
        pa = PayoutAccount.objects.create(
            user=child_user,
            type=PayoutAccount.Type.ESEWA,
            phone="9811111111",
        )
        r_wd = self.client.post(
            "/api/portal/child/wallet/withdraw/",
            {"amount": "5", "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r_wd.status_code, status.HTTP_201_CREATED)
        cw.refresh_from_db()
        self.assertEqual(cw.balance, Decimal("35.00"))

        perm.allow_cash_withdrawal = False
        perm.save()
        r_denied = self.client.post(
            "/api/portal/child/wallet/withdraw/",
            {"amount": "1", "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r_denied.status_code, status.HTTP_403_FORBIDDEN)

    def test_child_wallet_withdraw_requires_otp_when_enabled(self):
        ws = WalletSettings.load()
        ws.otp_for_withdrawals = True
        ws.save(update_fields=["otp_for_withdrawals"])
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        perm, _ = FamilyGroupPermission.objects.get_or_create(group=group)
        perm.allow_cash_withdrawal = True
        perm.save()
        child_user = User.objects.create_user(
            username="child_otp_wd",
            password=self.pw,
            phone="9712222222",
            name="ChildOtpWd",
            role=User.Role.CHILD,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        FamilyMember.objects.create(
            group=group,
            user=child_user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        Wallet.objects.create(
            owner=child_user,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
            balance=Decimal("20.00"),
        )
        self.client.credentials()
        login = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": child_user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(login.status_code, status.HTTP_200_OK)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        pa = PayoutAccount.objects.create(
            user=child_user,
            type=PayoutAccount.Type.ESEWA,
            phone="9812222222",
        )
        r0 = self.client.post(
            "/api/portal/child/wallet/withdraw/",
            {"amount": "3", "payout_account_id": pa.pk},
            format="json",
        )
        self.assertEqual(r0.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(r0.data.get("code"), "otp_required")
        row = otp_service.create_otp(child_user.phone, OTPVerification.Purpose.WITHDRAW)
        r1 = self.client.post(
            "/api/portal/child/wallet/withdraw/",
            {"amount": "3", "payout_account_id": pa.pk, "otp": row.otp},
            format="json",
        )
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED, r1.data)

    def test_family_parent_wallet_withdraw_multipart_notifications(self):
        self.leader.kyc_status = User.KYCStatus.VERIFIED
        self.leader.save(update_fields=["kyc_status"])
        User.objects.create_user(
            username="sadmin_wd",
            password=self.pw,
            phone="9822222222",
            name="SAdmin WD",
            role=User.Role.SUPER_ADMIN,
            is_superuser=True,
            is_staff=True,
        )
        self._login_leader_with_family()
        leader_token = Token.objects.get(user=self.leader).key
        group = FamilyGroup.objects.get(leader=self.leader)
        shared = get_default_shared_wallet(group)
        self.assertIsNotNone(shared)
        self._fund_family_master("500")
        pa = PayoutAccount.objects.create(
            user=self.leader,
            type=PayoutAccount.Type.ESEWA,
            phone="9800111222",
        )
        png_1x1 = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        img = SimpleUploadedFile("proof.png", png_1x1, content_type="image/png")
        r = self.client.post(
            "/api/portal/family/wallet/withdrawals/",
            {
                "wallet_id": str(shared.pk),
                "amount": "100",
                "payout_account_id": str(pa.pk),
                "proof_image": img,
            },
            format="multipart",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.data)
        wd = WalletWithdrawal.objects.order_by("-id").first()
        self.assertIsNotNone(wd)
        self.assertTrue(wd.proof_image)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.leader,
                title__icontains="submitted",
            ).exists()
        )
        self.assertEqual(
            Notification.objects.filter(
                recipient__is_superuser=True,
                title__icontains="withdrawal",
            ).count(),
            1,
        )

        admin = User.objects.get(username="sadmin_wd")
        tok, _ = Token.objects.get_or_create(user=admin)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r_ap = self.client.patch(
            f"/api/admin/withdrawals/{wd.pk}/",
            {"status": "approved"},
            format="json",
        )
        self.assertEqual(r_ap.status_code, status.HTTP_200_OK, r_ap.data)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.leader,
                title__icontains="approved",
            ).exists()
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Token {leader_token}")
        self._fund_family_master("200")
        pa2 = PayoutAccount.objects.create(
            user=self.leader,
            type=PayoutAccount.Type.ESEWA,
            phone="9800111333",
        )
        r2 = self.client.post(
            "/api/portal/family/wallet/withdrawals/",
            {
                "wallet_id": str(shared.pk),
                "amount": "50",
                "payout_account_id": str(pa2.pk),
            },
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        wd2 = WalletWithdrawal.objects.exclude(pk=wd.pk).order_by("-id").first()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r_rj = self.client.patch(
            f"/api/admin/withdrawals/{wd2.pk}/",
            {"status": "rejected", "reject_reason": "Test reject note"},
            format="json",
        )
        self.assertEqual(r_rj.status_code, status.HTTP_200_OK)
        n_rej = Notification.objects.filter(
            recipient=self.leader,
            title__icontains="rejected",
        ).order_by("-id").first()
        self.assertIsNotNone(n_rej)
        self.assertIn("Test reject note", n_rej.message)

    def test_child_rules_read_only(self):
        self._login_leader_with_family()
        group = FamilyGroup.objects.get(leader=self.leader)
        cat = Category.objects.filter(status=Category.Status.ACTIVE).first()
        if not cat:
            cat = Category.objects.create(
                name="TestCatR",
                slug=f"test-cr-{secrets.token_hex(4)}",
                status=Category.Status.ACTIVE,
            )
        ProductRestriction.objects.create(
            group=group,
            family_member=None,
            category=cat,
            is_blocked=True,
        )
        child_user = User.objects.create_user(
            username="childrules",
            password=self.pw,
            phone="9700000000",
            name="ChildRules",
            role=User.Role.CHILD,
        )
        FamilyMember.objects.create(
            group=group,
            user=child_user,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        Wallet.objects.create(
            owner=child_user,
            type=Wallet.Type.CHILD,
            family_group=group,
            status=Wallet.Status.ACTIVE,
        )
        self.client.credentials()
        login = self.client.post(
            "/api/child-portal/auth/login/",
            {"phone": child_user.phone, "password": self.pw},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {login.data['token']}")
        r = self.client.get("/api/portal/child/rules/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data["group_permissions"]["allow_online_purchases"])
        self.assertEqual(len(r.data["product_restrictions"]), 1)
        self.assertTrue(r.data["product_restrictions"][0]["is_blocked"])

    def test_child_member_adopts_personal_wallet_preserves_balance_and_checkout(self):
        group = FamilyGroup.objects.create(
            name="Adopt Fam",
            leader=self.leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        pw = get_or_create_personal_wallet(self.child)
        Wallet.objects.filter(pk=pw.pk).update(balance=Decimal("1500.00"))
        wallet_pk_before = pw.pk
        User.objects.filter(pk=self.child.pk).update(role=User.Role.CHILD)
        self.child.refresh_from_db()
        FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        family_service.ensure_family_wallets_for_member(
            group, self.child, FamilyMember.Role.CHILD
        )
        pw.refresh_from_db()
        self.assertEqual(pw.pk, wallet_pk_before)
        self.assertEqual(pw.type, Wallet.Type.CHILD)
        self.assertEqual(pw.family_group_id, group.pk)
        self.assertEqual(pw.balance, Decimal("1500.00"))
        cw = get_member_family_wallet(group, self.child)
        self.assertIsNotNone(cw)
        self.assertEqual(cw.pk, pw.pk)
        self.assertEqual(get_or_create_personal_wallet(self.child).pk, wallet_pk_before)

        tok, _ = Token.objects.get_or_create(user=self.child)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.get("/api/portal/orders/checkout-wallet/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsNotNone(r.data["default"])
        self.assertEqual(r.data["default"]["id"], wallet_pk_before)
        self.assertEqual(float(r.data["default"]["balance"]), 1500.0)

    def test_child_adoption_drops_empty_duplicate_child_wallet(self):
        group = FamilyGroup.objects.create(
            name="Dup Fam",
            leader=self.leader,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        dup = Wallet.objects.create(
            owner=self.child,
            type=Wallet.Type.CHILD,
            label="Child wallet",
            family_group=group,
            status=Wallet.Status.ACTIVE,
        )
        pw = get_or_create_personal_wallet(self.child)
        Wallet.objects.filter(pk=pw.pk).update(balance=Decimal("99.50"))
        User.objects.filter(pk=self.child.pk).update(role=User.Role.CHILD)
        self.child.refresh_from_db()
        FamilyMember.objects.create(
            group=group,
            user=self.child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        family_service.ensure_family_wallets_for_member(
            group, self.child, FamilyMember.Role.CHILD
        )
        self.assertFalse(Wallet.objects.filter(pk=dup.pk).exists())
        pw.refresh_from_db()
        self.assertEqual(pw.type, Wallet.Type.CHILD)
        self.assertEqual(pw.balance, Decimal("99.50"))

    def test_create_private_family_adopts_leader_personal_as_parent_wallet(self):
        rich = User.objects.create_user(
            username="richlead",
            password=self.pw,
            phone="9822222222",
            name="Rich Lead",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        w = get_or_create_personal_wallet(rich)
        Wallet.objects.filter(pk=w.pk).update(balance=Decimal("2000.00"))
        w_pk = w.pk
        g = family_service.create_family_group_for_user(rich, "Rich Home")
        w.refresh_from_db()
        self.assertEqual(w.pk, w_pk)
        self.assertEqual(w.type, Wallet.Type.PARENT)
        self.assertEqual(w.family_group_id, g.pk)
        self.assertEqual(w.balance, Decimal("2000.00"))
        self.assertTrue(
            Wallet.objects.filter(
                owner=rich, family_group=g, type=Wallet.Type.SHARED
            ).exists()
        )


class PortalSignupOtpAndTransferPolicyTests(TestCase):
    def setUp(self):
        relax_wallet_settings_for_tests()
        self.client = APIClient()

    def test_otp_signup_rejects_family_portal_at_send(self):
        r = self.client.post(
            "/api/auth/otp/send/",
            {
                "phone": "9817654321",
                "purpose": "signup",
                "name": "Head",
                "portal": "family-portal",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_otp_signup_creates_normal_member_only(self):
        phone = "9817654321"
        self.assertFalse(User.objects.filter(phone=phone).exists())
        r = self.client.post(
            "/api/auth/otp/send/",
            {
                "phone": phone,
                "purpose": "signup",
                "name": "Head",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        otp = OTPVerification.objects.filter(phone=phone, purpose="signup").latest(
            "created_at"
        ).otp
        r2 = self.client.post(
            "/api/auth/otp/verify/",
            {
                "phone": phone,
                "otp": otp,
                "purpose": "signup",
                "name": "Head",
            },
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertEqual(r2.data.get("portal"), "portal")
        u = User.objects.get(phone=phone)
        self.assertEqual(u.role, User.Role.NORMAL)
        self.assertFalse(FamilyGroup.objects.filter(leader=u).exists())

    def test_otp_signup_child_portal_rejected_on_send(self):
        r = self.client.post(
            "/api/auth/otp/send/",
            {
                "phone": "9817654322",
                "purpose": "signup",
                "name": "Kid",
                "portal": "child-portal",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wallet_transfer_rejects_non_family_recipient(self):
        pw = "Abcd1234!"
        a = User.objects.create_user(
            username="twa",
            password=pw,
            phone="9811111991",
            name="A",
            role=User.Role.NORMAL,
        )
        b = User.objects.create_user(
            username="twb",
            password=pw,
            phone="9811111992",
            name="B",
            role=User.Role.NORMAL,
        )
        tok, _ = Token.objects.get_or_create(user=a)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        wa = get_or_create_personal_wallet(a)
        wallet_service.credit_wallet(
            wa,
            Decimal("200"),
            wtype=WalletTransaction.Type.TOPUP,
            description="t",
            performed_by=a,
        )
        r = self.client.post(
            "/api/portal/wallet/transfer/",
            {"recipient": str(b.pk), "amount": "10"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        err = (r.data.get("recipient") or [""])[0]
        self.assertIn("allowed", err.lower())

    def test_family_overview_includes_member_monthly_spending(self):
        pw = "Abcd1234!"
        leader = User.objects.create_user(
            username="spl",
            password=pw,
            phone="9811111881",
            name="L",
            role=User.Role.NORMAL,
            kyc_status=User.KYCStatus.VERIFIED,
        )
        child = User.objects.create_user(
            username="spc",
            password=pw,
            phone="9811111882",
            name="C",
            role=User.Role.CHILD,
        )
        g = family_service.create_family_group_for_user(leader, "SpendTest")
        FamilyMember.objects.create(
            group=g,
            user=child,
            role=FamilyMember.Role.CHILD,
            status=FamilyMember.Status.ACTIVE,
        )
        family_service.ensure_family_wallets_for_member(
            g, child, FamilyMember.Role.CHILD
        )
        lw = get_member_family_wallet(g, leader)
        self.assertIsNotNone(lw)
        wallet_service.credit_wallet(
            lw,
            Decimal("500"),
            wtype=WalletTransaction.Type.TOPUP,
            description="t",
            performed_by=leader,
        )
        wallet_service.debit_wallet(
            lw,
            Decimal("50"),
            wtype=WalletTransaction.Type.PURCHASE,
            description="buy",
            performed_by=leader,
        )
        tok, _ = Token.objects.get_or_create(user=leader)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok.key}")
        r = self.client.get("/api/portal/family/members/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        leader_row = next(x for x in r.data["members"] if x["phone"] == leader.phone)
        self.assertGreaterEqual(float(leader_row["spending"]), 50.0)
