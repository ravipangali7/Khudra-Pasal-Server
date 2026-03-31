"""Support tickets, messages, and notifications."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    FamilyGroup,
    FamilyMember,
    Notification,
    SupportTicket,
    SupportTicketMessage,
    User,
    Vendor,
)


class SupportTicketApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.pw = "TestPass123!"
        self.customer = User.objects.create_user(
            username="st_cust",
            password=self.pw,
            phone="9711111111",
            name="Cust",
            role=User.Role.NORMAL,
        )
        self.parent = User.objects.create_user(
            username="st_par",
            password=self.pw,
            phone="9722222222",
            name="Par",
            role=User.Role.PARENT,
        )
        FamilyGroup.objects.create(
            name="ST Fam",
            leader=self.parent,
            type=FamilyGroup.Type.FAMILY,
            status=FamilyGroup.Status.ACTIVE,
        )
        self.admin = User.objects.create_user(
            username="st_adm",
            password=self.pw,
            phone="9733333333",
            name="Adm",
            role=User.Role.SUPER_ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.other = User.objects.create_user(
            username="st_other",
            password=self.pw,
            phone="9744444444",
            name="Other",
            role=User.Role.NORMAL,
        )
        self.vendor_user = User.objects.create_user(
            username="st_ven",
            password=self.pw,
            phone="9755555555",
            name="Ven",
            role=User.Role.NORMAL,
        )
        Vendor.objects.create(
            user=self.vendor_user,
            store_name="ST Store",
            store_slug="st-store-slug",
        )

    def _portal_token(self, user: User) -> str:
        r = self.client.post(
            "/api/portal/auth/login/",
            {"phone": user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def _family_token(self, user: User) -> str:
        r = self.client.post(
            "/api/family-portal/auth/login/",
            {"phone": user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def _admin_token(self) -> str:
        r = self.client.post(
            "/api/admin/auth/login/",
            {"phone": self.admin.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        return r.data["token"]

    def _vendor_token(self) -> str:
        r = self.client.post(
            "/api/vendor/auth/login/",
            {"phone": self.vendor_user.phone, "password": self.pw},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["token"]

    def test_portal_create_sets_customer_panel_and_message(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {
                "subject": "Hello",
                "description": "Need help",
                "category": "orders",
            },
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        tid = r.data["id"]
        t = SupportTicket.objects.get(ticket_number=tid)
        self.assertEqual(t.source_panel, SupportTicket.SourcePanel.CUSTOMER)
        self.assertEqual(t.category, SupportTicket.Category.ORDERS)
        self.assertEqual(t.messages.count(), 1)

        d = self.client.get(f"/api/portal/support/tickets/{tid}/")
        self.assertEqual(d.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(d.data.get("messages", [])), 1)

        n_before = Notification.objects.filter(type=Notification.Type.SUPPORT).count()
        r2 = self.client.post(
            f"/api/portal/support/tickets/{tid}/messages/",
            {"body": "Follow up"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertGreater(Notification.objects.filter(type=Notification.Type.SUPPORT).count(), n_before)

    def test_family_portal_user_gets_family_source_panel(self):
        tok = self._family_token(self.parent)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Fam", "description": "Fam issue"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        t = SupportTicket.objects.get(ticket_number=r.data["id"])
        self.assertEqual(t.source_panel, SupportTicket.SourcePanel.FAMILY)

    def test_vendor_ticket_source_panel(self):
        tok = self._vendor_token()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/vendor/support/tickets/",
            {"subject": "V subj", "description": "V desc"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        t = SupportTicket.objects.get(ticket_number=r.data["id"])
        self.assertEqual(t.source_panel, SupportTicket.SourcePanel.VENDOR)

    def test_stranger_cannot_post_message(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "X", "description": "Y"},
            format="json",
        )
        tid = r.data["id"]
        tok2 = self._portal_token(self.other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok2}")
        r2 = self.client.post(
            f"/api/portal/support/tickets/{tid}/messages/",
            {"body": "hack"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_reply_notifies_submitter(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Need admin", "description": "Please reply"},
            format="json",
        )
        tid = r.data["id"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._admin_token()}")
        n0 = Notification.objects.filter(recipient=self.customer, type=Notification.Type.SUPPORT).count()
        r2 = self.client.post(
            f"/api/admin/tickets/{tid}/messages/",
            {"body": "We are on it"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertGreater(
            Notification.objects.filter(recipient=self.customer, type=Notification.Type.SUPPORT).count(),
            n0,
        )
        last = (
            Notification.objects.filter(recipient=self.customer, type=Notification.Type.SUPPORT)
            .order_by("-pk")
            .first()
        )
        self.assertIsNotNone(last)
        self.assertIn("ticket=", last.action_url)
        self.assertIn(tid, last.action_url)
        self.assertTrue(last.action_url.startswith("/portal/support"))

        r3 = self.client.patch(
            f"/api/admin/tickets/{tid}/",
            {"status": SupportTicket.Status.IN_PROGRESS},
            format="json",
        )
        self.assertEqual(r3.status_code, status.HTTP_200_OK)
        t = SupportTicket.objects.get(ticket_number=tid)
        self.assertEqual(t.status, SupportTicket.Status.IN_PROGRESS)

    def test_portal_multipart_message_with_image_attachment(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Att", "description": "Need upload"},
            format="json",
        )
        tid = r.data["id"]
        img = SimpleUploadedFile(
            "note.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF", content_type="image/jpeg"
        )
        r2 = self.client.post(
            f"/api/portal/support/tickets/{tid}/messages/",
            {"body": "See image", "files": img},
            format="multipart",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED, r2.content)
        self.assertIn("message", r2.data)
        self.assertEqual(len(r2.data["message"]["attachments"]), 1)
        att_id = int(r2.data["message"]["attachments"][0]["id"])
        r3 = self.client.get(f"/api/portal/support/attachments/{att_id}/")
        self.assertEqual(r3.status_code, status.HTTP_200_OK)

    def test_portal_messages_pagination_before(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Pag", "description": "Line one"},
            format="json",
        )
        tid = r.data["id"]
        t = SupportTicket.objects.get(ticket_number=tid)
        first_msg = t.messages.order_by("pk").first()
        self.assertIsNotNone(first_msg)
        r2 = self.client.get(
            f"/api/portal/support/tickets/{tid}/messages/",
            {"before": str(first_msg.pk), "limit": 10},
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertIn("results", r2.data)
        self.assertIn("has_more", r2.data)

    def test_stranger_cannot_download_attachment(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Z", "description": "D"},
            format="json",
        )
        tid = r.data["id"]
        img = SimpleUploadedFile("x.png", b"\x89PNG\r\n\x1a\n", content_type="image/png")
        r2 = self.client.post(
            f"/api/portal/support/tickets/{tid}/messages/",
            {"files": img},
            format="multipart",
        )
        att_id = int(r2.data["message"]["attachments"][0]["id"])
        tok2 = self._portal_token(self.other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok2}")
        r3 = self.client.get(f"/api/portal/support/attachments/{att_id}/")
        self.assertEqual(r3.status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_support_notification_deep_links_ticket(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Ping admins", "description": "Hello"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        tid = r.data["id"]
        n = (
            Notification.objects.filter(recipient=self.admin, type=Notification.Type.SUPPORT)
            .order_by("-pk")
            .first()
        )
        self.assertIsNotNone(n)
        self.assertIn("ticket=", n.action_url)
        self.assertIn(tid, n.action_url)
        self.assertTrue(n.action_url.startswith("/admin/support-tickets"))

    def test_vendor_support_notification_deep_links_ticket(self):
        tok = self._vendor_token()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/vendor/support/tickets/",
            {"subject": "V issue", "description": "Desc"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        tid = r.data["id"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self._admin_token()}")
        r2 = self.client.post(
            f"/api/admin/tickets/{tid}/messages/",
            {"body": "Staff here"},
            format="json",
        )
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        n = (
            Notification.objects.filter(recipient=self.vendor_user, type=Notification.Type.SUPPORT)
            .order_by("-pk")
            .first()
        )
        self.assertIsNotNone(n)
        self.assertIn("ticket=", n.action_url)
        self.assertIn(tid, n.action_url)
        self.assertTrue(n.action_url.startswith("/vendor/tickets"))

    def test_reject_disallowed_extension(self):
        tok = self._portal_token(self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        r = self.client.post(
            "/api/portal/support/tickets/",
            {"subject": "Bad", "description": "X"},
            format="json",
        )
        tid = r.data["id"]
        bad = SimpleUploadedFile("x.exe", b"MZ", content_type="application/octet-stream")
        r2 = self.client.post(
            f"/api/portal/support/tickets/{tid}/messages/",
            {"body": "hack", "files": bad},
            format="multipart",
        )
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)
