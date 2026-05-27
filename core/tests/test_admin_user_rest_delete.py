from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from core.models import EmployeeProfile, Role, User


class AdminUserRestDeleteTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.super_admin = User.objects.create_user(
            username="sa_delete",
            password=self.pw,
            phone="9802000001",
            name="Super Admin",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff_no_delete = User.objects.create_user(
            username="staff_nodelete",
            password=self.pw,
            phone="9802000002",
            name="Staff No Delete",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        staff_role = Role.objects.create(
            name="Customers Admin",
            permissions={"customers": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff_no_delete,
            role=staff_role,
            modules_access=["dashboard", "customers"],
            status=EmployeeProfile.Status.ACTIVE,
        )
        self.customer = User.objects.create_user(
            username="cust_target",
            password=self.pw,
            phone="9802000003",
            name="Customer Target",
            role=User.Role.NORMAL,
            is_staff=False,
            is_superuser=False,
        )

    def _token(self, user: User) -> str:
        tok, _ = Token.objects.get_or_create(user=user)
        return tok.key

    def _detail_url(self, user_id: int) -> str:
        return reverse("admin-users-write", kwargs={"pk": user_id})

    def test_super_admin_can_delete_customer(self):
        target_id = self.customer.pk
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        response = self.client.delete(self._detail_url(target_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(User.objects.filter(pk=target_id).exists())

    def test_staff_without_delete_permission_forbidden(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.staff_no_delete)}")
        response = self.client.delete(self._detail_url(self.customer.pk))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.customer.pk).exists())

    def test_cannot_delete_self(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        response = self.client.delete(self._detail_url(self.super_admin.pk))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.super_admin.pk).exists())

    def test_cannot_delete_other_superuser(self):
        other_super = User.objects.create_user(
            username="other_super",
            password=self.pw,
            phone="9802000004",
            name="Other Super",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        response = self.client.delete(self._detail_url(other_super.pk))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=other_super.pk).exists())

    def test_super_admin_can_delete_staff_admin(self):
        target = User.objects.create_user(
            username="staff_target",
            password=self.pw,
            phone="9802000005",
            name="Staff Target",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        target_id = target.pk
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        response = self.client.delete(self._detail_url(target_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertFalse(User.objects.filter(pk=target_id).exists())

    def test_cannot_delete_super_admin_role_without_superuser_flag(self):
        target = User.objects.create_user(
            username="sa_role_only",
            password=self.pw,
            phone="9802000006",
            name="SA Role Only",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=False,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(self.super_admin)}")
        response = self.client.delete(self._detail_url(target.pk))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(User.objects.filter(pk=target.pk).exists())
