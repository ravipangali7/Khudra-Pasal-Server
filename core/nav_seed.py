"""
Seed tuples: (surface, key, label, icon, parent_key, sort_order, badge_key, roles_filter[, view_key]).
Optional 9th field `view_key` is the frontend screen id (empty = use key). Only required where key != screen.
surface: admin | vendor | portal_main | portal_family | portal_child

Apply to the database with: python manage.py seed_navigation
"""

NavRow = tuple[str, str, str, str, str, int, str, str, str]


def _row9(row: tuple) -> NavRow:
    if len(row) == 9:
        return row  # type: ignore[return-value]
    surface, key, label, icon, parent_key, sort_order, badge_key, roles_filter = row[:8]
    return (surface, key, label, icon, parent_key, sort_order, badge_key, roles_filter, "")

ADMIN_NAV: list[tuple] = [
    ("admin", "dashboard", "Dashboard", "Home", "", 0, "", ""),
    ("admin", "pos", "POS System", "ShoppingCart", "", 10, "", ""),
    ("admin", "catalog", "Catalog", "FolderTree", "", 20, "", ""),
    ("admin", "categories", "Categories", "Layers", "catalog", 0, "", ""),
    ("admin", "subcategories", "Sub Categories", "Layers", "catalog", 1, "", ""),
    ("admin", "child-categories", "Child Categories", "Layers", "catalog", 2, "", ""),
    ("admin", "brands", "Brands", "Tag", "catalog", 3, "", ""),
    ("admin", "attributes", "Attributes", "Palette", "catalog", 4, "", ""),
    ("admin", "units", "Units", "Ruler", "catalog", 5, "", ""),
    ("admin", "products", "Products", "Package", "", 30, "", ""),
    ("admin", "all-products", "All Products", "Package", "products", 0, "", ""),
    ("admin", "inhouse", "In-House Products", "Building2", "products", 1, "", ""),
    ("admin", "product-approvals", "Approval Queue", "ClipboardList", "products", 2, "", ""),
    ("admin", "reviews", "Product Reviews", "Star", "products", 3, "", ""),
    ("admin", "po-billing", "PO & Billing", "Receipt", "", 40, "", ""),
    ("admin", "orders", "Orders", "ShoppingCart", "", 50, "admin_pending_orders", ""),
    ("admin", "orders-all", "All Orders", "ShoppingCart", "orders", 0, "", ""),
    ("admin", "orders-settings", "Order Settings", "Settings", "orders", 1, "", ""),
    ("admin", "marketing", "Marketing", "Megaphone", "", 60, "", ""),
    ("admin", "banners", "Banners", "Image", "marketing", 0, "", ""),
    ("admin", "flash-deals", "Flash Deals", "Clock", "marketing", 1, "", ""),
    ("admin", "coupons", "Coupons", "Ticket", "marketing", 2, "", ""),
    ("admin", "notifications", "Notifications", "Bell", "marketing", 3, "", ""),
    ("admin", "cms", "CMS", "FileText", "", 70, "", ""),
    ("admin", "finance", "Finance", "CreditCard", "", 80, "admin_finance_attention", ""),
    ("admin", "transactions", "Transactions", "CreditCard", "finance", 0, "", ""),
    ("admin", "refunds", "Refunds", "RefreshCw", "finance", 1, "admin_pending_refunds", ""),
    ("admin", "withdrawals", "Withdrawals Requesting", "CreditCard", "finance", 2, "admin_pending_withdrawals", ""),
    ("admin", "commission-log", "Commission log", "Percent", "finance", 3, "", ""),
    ("admin", "users", "Users", "Users", "", 90, "", ""),
    ("admin", "admins", "Administrators", "UserCog", "users", 0, "", ""),
    ("admin", "customers", "Customers", "Users", "users", 1, "", ""),
    ("admin", "sellers", "Sellers / Vendors", "Building2", "users", 2, "", ""),
    ("admin", "users-kyc", "KYC Verification", "Shield", "users", 3, "", ""),
    ("admin", "employees", "Employees & Roles", "UserCheck", "", 100, "", ""),
    ("admin", "employees-all", "All Employees", "Users", "employees", 0, "", ""),
    ("admin", "roles", "Roles & Permissions", "Shield", "employees", 1, "", ""),
    ("admin", "audit-logs", "Audit Logs", "ClipboardList", "employees", 2, "", ""),
    ("admin", "delivery", "Delivery Men", "Truck", "", 110, "", ""),
    ("admin", "families", "Families & Groups", "Building2", "", 120, "", ""),
    ("admin", "families-all", "All Groups", "Building2", "families", 0, "", ""),
    ("admin", "families-wallets", "Wallet Control", "Wallet", "families", 1, "", ""),
    ("admin", "wallet-master", "Wallet Master", "Wallet", "", 130, "", ""),
    ("admin", "wallet-overview", "All Wallets", "Wallet", "wallet-master", 0, "", ""),
    ("admin", "wallet-bonus", "Wallet Bonus", "Gift", "wallet-master", 1, "", ""),
    ("admin", "wallet-loyalty", "Loyalty & Referral", "Award", "wallet-master", 2, "", ""),
    ("admin", "wallet-settings", "Wallet Settings", "Settings", "wallet-master", 3, "", ""),
    ("admin", "wallet-flagged", "Flagged Activity", "AlertTriangle", "wallet-master", 4, "", ""),
    ("admin", "support-tickets", "Support Tickets", "MessageSquare", "", 138, "", ""),
    ("admin", "security", "Security & Flags", "ShieldAlert", "", 140, "", ""),
    ("admin", "reports", "Reports", "BarChart3", "", 150, "", ""),
    ("admin", "reels-admin", "KhudraReels", "Play", "", 160, "", ""),
    ("admin", "shipping", "Shipping", "Truck", "", 170, "", ""),
    ("admin", "shipping-methods", "Shipping Methods", "Truck", "shipping", 0, "", ""),
    ("admin", "shipping-zones", "Zones & Rates", "Truck", "shipping", 1, "", ""),
    ("admin", "shipping-calculator", "Cost Calculator", "Truck", "shipping", 2, "", ""),
    ("admin", "settings", "Settings", "Settings", "", 180, "", ""),
]

VENDOR_NAV: list[tuple] = [
    ("vendor", "dashboard", "Dashboard", "Home", "", 0, "", ""),
    ("vendor", "store", "Store Profile", "Store", "", 10, "", ""),
    ("vendor", "products", "Products", "Package", "", 20, "vendor_pending_products", ""),
    ("vendor", "all-products", "All Products", "Package", "products", 0, "", ""),
    ("vendor", "add-product", "Add Product", "Plus", "products", 1, "", ""),
    ("vendor", "reviews", "Reviews", "Star", "products", 2, "", ""),
    ("vendor", "orders", "Orders", "ShoppingCart", "", 30, "vendor_pending_orders", ""),
    ("vendor", "all-orders", "All Orders", "ShoppingCart", "orders", 0, "", ""),
    ("vendor", "pending", "Pending", "Clock", "orders", 1, "", ""),
    ("vendor", "returns", "Returns", "RefreshCw", "orders", 2, "", ""),
    ("vendor", "pos", "POS System", "CreditCard", "", 40, "", ""),
    ("vendor", "inventory", "Inventory", "Warehouse", "", 45, "", ""),
    ("vendor", "suppliers", "Suppliers", "Truck", "inventory", 0, "", ""),
    ("vendor", "stock-purchases", "Stock purchases", "PackagePlus", "inventory", 1, "", ""),
    ("vendor", "ledger", "Ledger", "BookText", "inventory", 2, "", ""),
    ("vendor", "wallet", "Wallet & Finance", "Wallet", "", 60, "", ""),
    ("vendor", "earnings", "Earnings", "TrendingUp", "wallet", 0, "", ""),
    ("vendor", "kyc", "KYC Verification", "Shield", "wallet", 1, "", ""),
    ("vendor", "payout-accounts", "Payout accounts", "Landmark", "wallet", 2, "", ""),
    ("vendor", "withdrawals", "Withdrawals", "Banknote", "wallet", 3, "", ""),
    ("vendor", "transactions", "Transactions", "CreditCard", "wallet", 4, "", ""),
    ("vendor", "reels", "KhudraReels", "Film", "", 70, "", ""),
    ("vendor", "my-reels", "My Reels", "Film", "reels", 0, "", ""),
    ("vendor", "customers", "Customers", "Users", "", 80, "", ""),
    ("vendor", "reports", "Reports", "BarChart3", "", 90, "", ""),
    ("vendor", "support", "Support", "MessageSquare", "", 100, "", ""),
    ("vendor", "tickets", "Support Tickets", "MessageSquare", "support", 0, "", ""),
]

PORTAL_MAIN_NAV: list[tuple] = [
    ("portal_main", "dashboard", "Dashboard", "LayoutDashboard", "", 0, "portal_notifications", ""),
    ("portal_main", "wallet", "Wallet", "Wallet", "", 10, "", ""),
    ("portal_main", "wallet-payout-accounts", "Payout accounts", "Landmark", "", 11, "", "", "wallet-payout"),
    ("portal_main", "wallet-withdraw", "Withdraw", "ArrowUpRight", "", 12, "", "", "wallet-withdraw"),
    ("portal_main", "kyc", "KYC Verification", "Shield", "", 13, "", ""),
    ("portal_main", "child-accounts", "Child Accounts", "Users", "", 20, "", "parent"),
    ("portal_main", "family-wallet", "Family Wallet", "UsersRound", "", 30, "", "parent"),
    ("portal_main", "products", "Products", "Package", "", 35, "", ""),
    ("portal_main", "wishlist", "Wishlist", "Heart", "", 37, "", ""),
    ("portal_main", "orders", "Orders", "ShoppingBag", "", 40, "", ""),
    ("portal_main", "transactions", "Transactions", "Receipt", "", 50, "", ""),
    ("portal_main", "switch-portal", "Switch Portal", "RefreshCcw", "", 60, "", ""),
    ("portal_main", "profile", "Profile", "User", "", 70, "", ""),
    ("portal_main", "support", "Support", "HelpCircle", "", 80, "", ""),
]

PORTAL_FAMILY_NAV: list[tuple] = [
    ("portal_family", "dashboard", "Dashboard", "Home", "", 0, "", "", "dashboard"),
    ("portal_family", "members", "Family Members", "Users", "", 10, "", "", "members"),
    ("portal_family", "members-list", "All Members", "Users", "members", 0, "", "", "members"),
    ("portal_family", "members-requests", "Join Requests", "Clock", "members", 2, "", "", "members-requests"),
    (
        "portal_family",
        "product-approval-requests",
        "Product approval requests",
        "ClipboardList",
        "members",
        3,
        "family_pending_purchase_requests",
        "",
        "product-approval-requests",
    ),
    ("portal_family", "wallet-management", "Wallet Management", "Wallet", "", 20, "", "", "wallets"),
    ("portal_family", "wallets-overview", "Overview", "Wallet", "wallet-management", 0, "", "", "wallets"),
    ("portal_family", "wallets-payout-accounts", "Payout accounts", "Landmark", "wallet-management", 1, "", "", "wallets-payout"),
    ("portal_family", "wallets-withdraw", "Withdraw", "ArrowUpRight", "wallet-management", 2, "", "", "wallets-withdraw"),
    ("portal_family", "controls", "Spending Controls", "Shield", "", 30, "", "", "spending-limits"),
    ("portal_family", "controls-limits", "Limits", "ShieldCheck", "controls", 0, "", "", "spending-limits"),
    ("portal_family", "controls-restrictions", "Product Restrictions", "Lock", "controls", 1, "", "", "product-restrictions"),
    ("portal_family", "controls-auto-approval", "Auto-Approval Rules", "CheckCircle", "controls", 2, "", "", "auto-approval"),
    ("portal_family", "products", "Products", "Package", "", 35, "", "", "products"),
    ("portal_family", "my-orders", "My Orders", "ShoppingBag", "", 36, "", "", "my-orders"),
    ("portal_family", "history", "Transaction History", "History", "", 40, "", "", "history"),
    ("portal_family", "kyc", "KYC Verification", "Shield", "", 44, "", "", "kyc"),
    ("portal_family", "profile", "Profile", "User", "", 45, "", "", "profile"),
    ("portal_family", "support", "Support", "HelpCircle", "", 48, "", "", "support"),
]

PORTAL_CHILD_NAV: list[tuple] = [
    ("portal_child", "dashboard", "Dashboard", "Home", "", 0, "", ""),
    ("portal_child", "wallet-menu", "Wallet", "Wallet", "", 10, "", ""),
    ("portal_child", "wallet", "My Wallet", "Wallet", "wallet-menu", 0, "", "", "wallet"),
    ("portal_child", "topup", "Add Money", "ArrowDownLeft", "wallet-menu", 1, "", "", "topup"),
    ("portal_child", "transfer", "Transfer", "Send", "wallet-menu", 2, "", "", "transfer"),
    ("portal_child", "withdraw", "Withdraw", "ArrowUpRight", "wallet-menu", 3, "", "", "withdraw"),
    ("portal_child", "kyc", "KYC Verification", "Shield", "", 11, "", ""),
    ("portal_child", "products", "Products", "Package", "", 15, "", ""),
    ("portal_child", "my-orders", "My Orders", "ShoppingBag", "", 16, "", "", "my-orders"),
    ("portal_child", "payout-accounts", "Payout accounts", "Landmark", "", 17, "", ""),
    ("portal_child", "requests", "Pending Requests", "Clock", "", 50, "child_pending_purchase_requests", ""),
    ("portal_child", "history", "Transaction History", "History", "", 60, "", ""),
    ("portal_child", "rules", "Parent Rules", "Shield", "", 70, "", ""),
    ("portal_child", "help", "Help & Support", "HelpCircle", "", 80, "", ""),
    ("portal_child", "profile", "Profile", "User", "", 75, "", ""),
]


def all_seed_rows() -> list[NavRow]:
    raw = (
        ADMIN_NAV
        + VENDOR_NAV
        + PORTAL_MAIN_NAV
        + PORTAL_FAMILY_NAV
        + PORTAL_CHILD_NAV
    )
    return [_row9(r) for r in raw]
