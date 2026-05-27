from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from core.models import EmployeeProfile, Role, SecuritySettings, User


class AdminStaffUserUpdateTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        SecuritySettings.objects.update_or_create(
            pk=1,
            defaults={"rbac_enforced": True},
        )
        self.super_admin = User.objects.create_user(
            username="sa_staff_upd",
            password=self.pw,
            phone="9803000001",
            name="Super Admin",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.staff_users_admin = User.objects.create_user(
            username="staff_users_admin",
            password=self.pw,
            phone="9803000002",
            name="Users Admin",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        users_role = Role.objects.create(
            name="Users Admin Role",
            permissions={"users": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=self.staff_users_admin,
            role=users_role,
            modules_access=["dashboard", "users"],
            status=EmployeeProfile.Status.ACTIVE,
        )
        self.target_admin = User.objects.create_user(
            username="target_admin",
            password=self.pw,
            phone="9803000003",
            name="Target Admin",
            email="target@example.com",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )

    def _token(self, user: User) -> str:
        tok, _ = Token.objects.get_or_create(user=user)
        return tok.key

    def _detail_url(self, user_id: int) -> str:
        return reverse("admin-users-write", kwargs={"pk": user_id})

    def test_staff_with_users_permission_can_patch_admin_account(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self._token(self.staff_users_admin)}"
        )
        response = self.client.patch(
            self._detail_url(self.target_admin.pk),
            {
                "name": "Updated Admin Name",
                "email": "updated@example.com",
                "phone": "9803000003",
                "is_active": True,
                "is_staff": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.target_admin.refresh_from_db()
        self.assertEqual(self.target_admin.name, "Updated Admin Name")
        self.assertEqual(self.target_admin.email, "updated@example.com")

    def test_staff_without_users_permission_forbidden(self):
        staff_no_users = User.objects.create_user(
            username="staff_no_users",
            password=self.pw,
            phone="9803000004",
            name="Customers Only",
            role=User.Role.STAFF,
            is_staff=True,
            is_superuser=False,
        )
        customers_role = Role.objects.create(
            name="Customers Only Role",
            permissions={"customers": True},
            status=Role.Status.ACTIVE,
        )
        EmployeeProfile.objects.create(
            user=staff_no_users,
            role=customers_role,
            modules_access=["dashboard", "customers"],
            status=EmployeeProfile.Status.ACTIVE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._token(staff_no_users)}")
        response = self.client.patch(
            self._detail_url(self.target_admin.pk),
            {"name": "Should Fail"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.target_admin.refresh_from_db()
        self.assertEqual(self.target_admin.name, "Target Admin")
