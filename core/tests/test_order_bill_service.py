from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from core.models import Order
from core.services.order_bill_service import (
    ensure_order_bill,
    portal_bill_action_url,
    portal_orders_path_for_order,
)


class OrderBillPortalPathTests(SimpleTestCase):
    def _order(self, portal: str, pk: int = 42) -> Order:
        o = Order(pk=pk, order_number="ORD-TEST", placed_portal=portal, is_pos_order=False)
        return o

    def test_portal_paths_by_surface(self):
        main = self._order(Order.PlacedPortal.PORTAL_MAIN)
        self.assertEqual(portal_orders_path_for_order(main), "/portal/orders/42")
        self.assertTrue(portal_bill_action_url(main).endswith("?bill=1"))

        family = self._order(Order.PlacedPortal.PORTAL_FAMILY)
        self.assertIn("family-portal", portal_orders_path_for_order(family))

        child = self._order(Order.PlacedPortal.PORTAL_CHILD)
        self.assertIn("child-portal", portal_orders_path_for_order(child))

    @patch("core.services.order_bill_service.generate_order_bill_image")
    @patch("core.services.order_bill_service.Order.objects.filter")
    def test_ensure_order_bill_skips_when_already_present(self, mock_filter, mock_gen):
        order = MagicMock()
        order.bill_image = True
        order.is_pos_order = False
        order.placed_portal = Order.PlacedPortal.PORTAL_MAIN
        mock_filter.return_value.first.return_value = order
        ensure_order_bill(1)
        mock_gen.assert_not_called()

    @patch("core.services.order_bill_service.generate_order_bill_image")
    @patch("core.services.order_bill_service.Order.objects.filter")
    def test_ensure_order_bill_generates_for_portal_order(self, mock_filter, mock_gen):
        order = MagicMock()
        order.bill_image = None
        order.is_pos_order = False
        order.placed_portal = Order.PlacedPortal.PORTAL_MAIN
        mock_filter.return_value.first.return_value = order
        ensure_order_bill(1)
        mock_gen.assert_called_once_with(order)

    @patch("core.services.order_bill_service.generate_order_bill_image")
    @patch("core.services.order_bill_service.Order.objects.filter")
    def test_ensure_order_bill_generates_for_pos_order(self, mock_filter, mock_gen):
        order = MagicMock()
        order.bill_image = None
        order.is_pos_order = True
        order.placed_portal = ""
        mock_filter.return_value.first.return_value = order
        ensure_order_bill(1)
        mock_gen.assert_called_once_with(order)
