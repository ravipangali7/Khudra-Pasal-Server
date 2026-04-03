"""Product review POST requires delivered + paid order line for that product."""

from decimal import Decimal

from django.test import TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from core.models import Category, Order, OrderItem, Product, ProductReview, User, Vendor
from core.views.vendor.vendor_resources import _gen_order_number


class WebsiteReviewEligibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="revu1",
            password="x",
            phone="9855555555",
            name="Review User",
            role=User.Role.NORMAL,
        )
        self.token = Token.objects.create(user=self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

        self.vendor_user = User.objects.create_user(
            username="revv1",
            password="x",
            phone="9866666666",
            name="V",
            role=User.Role.NORMAL,
        )
        self.vendor = Vendor.objects.create(
            user=self.vendor_user,
            store_name="Rev Store",
            status=Vendor.Status.APPROVED,
        )
        self.cat = Category.objects.create(name="CRev", slug="c-rev")
        self.product = Product.objects.create(
            name="Rev Product",
            slug="rev-product-slug",
            sku="SKU-REV-1",
            category=self.cat,
            seller=self.vendor,
            price=Decimal("50.00"),
            stock=20,
            status=Product.Status.ACTIVE,
        )

    def _post_review(self):
        return self.client.post(
            f"/api/website/products/{self.product.slug}/reviews/",
            {"rating": 5, "comment": "Great"},
            format="json",
        )

    def test_post_review_rejected_without_delivered_paid_order(self):
        r = self._post_review()
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("delivered", str(r.data.get("detail", "")).lower())

    def test_post_review_allowed_with_delivered_paid_line_item(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=Decimal("50.00"),
            total_price=Decimal("50.00"),
        )
        r = self._post_review()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ProductReview.objects.filter(product=self.product, customer=self.user).exists())

    def test_post_review_rejected_when_duplicate(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=Decimal("50.00"),
            total_price=Decimal("50.00"),
        )
        r1 = self._post_review()
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        r2 = self._post_review()
        self.assertEqual(r2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_product_detail_includes_can_submit_review_when_eligible(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.WALLET,
            payment_status=Order.PaymentStatus.PAID,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=Decimal("50.00"),
            total_price=Decimal("50.00"),
        )
        r = self.client.get(f"/api/website/products/{self.product.slug}/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(r.data.get("can_submit_review"))

        ProductReview.objects.create(
            product=self.product,
            customer=self.user,
            rating=5,
            comment="x",
            status=ProductReview.Status.PENDING,
        )
        r2 = self.client.get(f"/api/website/products/{self.product.slug}/")
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        self.assertFalse(r2.data.get("can_submit_review"))

    def test_post_review_allowed_delivered_cod_payment_pending(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.COD,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=Decimal("50.00"),
            total_price=Decimal("50.00"),
        )
        r = self._post_review()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        r_detail = self.client.get(f"/api/website/products/{self.product.slug}/")
        self.assertEqual(r_detail.status_code, status.HTTP_200_OK)
        self.assertFalse(r_detail.data.get("can_submit_review"))

    def test_post_review_rejected_delivered_non_cod_pending_payment(self):
        order = Order.objects.create(
            order_number=_gen_order_number(),
            customer=self.user,
            seller=self.vendor,
            status=Order.Status.DELIVERED,
            payment_method=Order.PaymentMethod.ESEWA,
            payment_status=Order.PaymentStatus.PENDING,
            subtotal=Decimal("50.00"),
            delivery_fee=Decimal("0"),
            discount_amount=Decimal("0"),
            total=Decimal("50.00"),
        )
        OrderItem.objects.create(
            order=order,
            product=self.product,
            quantity=1,
            unit_price=Decimal("50.00"),
            total_price=Decimal("50.00"),
        )
        r = self._post_review()
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
