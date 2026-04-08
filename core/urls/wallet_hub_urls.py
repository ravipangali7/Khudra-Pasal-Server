from django.urls import path

from core.views.portal import wallet_hub_views

urlpatterns = [
    path(
        "transfer-id/me/",
        wallet_hub_views.wallet_hub_transfer_id_me,
        name="wallet-hub-transfer-id-me",
    ),
    path(
        "transfer-id/create/",
        wallet_hub_views.wallet_hub_transfer_id_create,
        name="wallet-hub-transfer-id-create",
    ),
    path(
        "transfer-id/<str:code>/",
        wallet_hub_views.wallet_hub_transfer_id_lookup,
        name="wallet-hub-transfer-id-lookup",
    ),
    path(
        "wallet/transfer/",
        wallet_hub_views.wallet_hub_wallet_transfer,
        name="wallet-hub-wallet-transfer",
    ),
]
