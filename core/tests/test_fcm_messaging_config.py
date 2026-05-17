"""Public FCM web config endpoint and token registration."""

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import User


class FirebaseMessagingConfigTests(TestCase):
    def test_firebase_messaging_exposes_vapid_key(self):
        res = APIClient().get("/api/website/firebase-messaging/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        self.assertTrue(body.get("vapid_key"))
        self.assertIn("firebase_configured", body)


class AuthFcmTokenTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="fcm_u",
            password="x",
            phone="9812345678",
            name="FCM User",
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def test_register_fcm_token(self):
        res = self.client.post(
            "/api/auth/fcm-token/",
            {"fcm_token": "test-device-token-abc"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.fcm_token, "test-device-token-abc")

    def test_register_fcm_token_clears_other_users(self):
        other = User.objects.create_user(
            username="fcm_other",
            password="x",
            phone="9812345679",
            name="Other",
        )
        other.fcm_token = "shared-device-token"
        other.save(update_fields=["fcm_token"])

        res = self.client.post(
            "/api/auth/fcm-token/",
            {"fcm_token": "shared-device-token"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        other.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(other.fcm_token, "")
        self.assertEqual(self.user.fcm_token, "shared-device-token")
