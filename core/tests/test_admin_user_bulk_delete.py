from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from core.models import User


class AdminUserBulkDeleteTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.password = "TestPass123!"
        self.changelist_url = reverse("admin:core_user_changelist")

    def _mk_staff(self, username: str, phone: str, role=User.Role.STAFF, is_superuser=False):
        return User.objects.create_user(
            username=username,
            password=self.password,
            phone=phone,
            name=username,
            role=role,
            is_staff=True,
            is_superuser=is_superuser,
            is_active=True,
        )

    def test_delete_selected_visible_with_delete_permission(self):
        admin_user = self._mk_staff("admin_with_delete", "9801000001")
        delete_perm = Permission.objects.get(codename="delete_user")
        view_perm = Permission.objects.get(codename="view_user")
        admin_user.user_permissions.add(delete_perm, view_perm)

        self.client.force_login(admin_user)
        response = self.client.get(self.changelist_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="delete_selected"')

    def test_delete_selected_hidden_without_delete_permission(self):
        admin_user = self._mk_staff("admin_without_delete", "9801000002")
        view_perm = Permission.objects.get(codename="view_user")
        admin_user.user_permissions.add(view_perm)

        self.client.force_login(admin_user)
        response = self.client.get(self.changelist_url)

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="delete_selected"')

    def test_bulk_delete_skips_self_and_superusers(self):
        deleter = self._mk_staff("deleter_staff", "9801000003")
        delete_perm = Permission.objects.get(codename="delete_user")
        view_perm = Permission.objects.get(codename="view_user")
        deleter.user_permissions.add(delete_perm, view_perm)

        super_admin = self._mk_staff(
            "super_target",
            "9801000004",
            role=User.Role.SUPER_ADMIN,
            is_superuser=True,
        )
        normal_target = User.objects.create_user(
            username="normal_target",
            password=self.password,
            phone="9801000005",
            name="Normal Target",
            role=User.Role.NORMAL,
            is_staff=False,
            is_superuser=False,
            is_active=True,
        )

        self.client.force_login(deleter)
        response = self.client.post(
            self.changelist_url,
            {
                "action": "delete_selected",
                "_selected_action": [str(deleter.pk), str(super_admin.pk), str(normal_target.pk)],
                "post": "yes",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(pk=normal_target.pk).exists())
        self.assertTrue(User.objects.filter(pk=deleter.pk).exists())
        self.assertTrue(User.objects.filter(pk=super_admin.pk).exists())
