from __future__ import annotations

"""
Delivery staff earnings: Order has no delivery assignee FK yet.
When the model links a delivery person, extend on_order_delivered to update stats.
"""

from core.models import Order


def on_order_delivered(order: Order, delivery_man=None) -> None:
    if delivery_man is None:
        return
    # Placeholder for future Order.delivery_man assignment
    _ = order
