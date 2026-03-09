from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "possumus.settings")

import django

django.setup()

from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from pos.models import Customer, Product, Purchase, Sale, Supplier
from pos.services.access import is_admin


HTML_DIR = ROOT / "docs" / "snapshots" / "html"


def pick_user():
    User = get_user_model()
    users = User.objects.filter(is_active=True).order_by("id")
    for user in users:
        if is_admin(user):
            return user
    return users.first()


def snapshot_targets():
    targets = [
        ("dashboard", "/dashboard/"),
        ("pos", "/pos/"),
        ("products", "/products/"),
        ("stock_control", "/inventory/adjustments/"),
        ("purchases", "/purchases/"),
        ("expenses", "/accounts/expenses/"),
        ("money_desk", "/accounts/money/"),
        ("parties", "/parties/"),
        ("customers", "/parties/customers/"),
        ("suppliers", "/parties/suppliers/"),
        ("accounts", "/accounts/"),
        ("money_breakdown", "/accounts/money-breakdown/"),
        ("reports", "/reports/"),
        ("daily_sales_report", "/reports/daily-sales/"),
    ]

    first_customer = Customer.objects.order_by("id").first()
    if first_customer:
        targets.append(("customer_detail", f"/parties/customer/{first_customer.id}/"))

    first_supplier = Supplier.objects.order_by("id").first()
    if first_supplier:
        targets.append(("supplier_detail", f"/parties/supplier/{first_supplier.id}/"))

    first_product = Product.objects.order_by("id").first()
    if first_product:
        targets.append(("product_detail", f"/products/{first_product.id}/"))

    first_purchase = Purchase.objects.order_by("id").first()
    if first_purchase:
        targets.append(("purchase_detail", f"/purchases/{first_purchase.id}/"))

    first_sale = Sale.objects.order_by("id").first()
    if first_sale:
        targets.append(("invoice", f"/invoice/{first_sale.id}/"))

    return targets


def main():
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    user = pick_user()
    if not user:
        raise SystemExit("No active user found. Create an admin user before capturing snapshots.")

    with override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"]):
        client = Client()
        client.force_login(user)

        for name, url in snapshot_targets():
            response = client.get(url, follow=True)
            if response.status_code != 200:
                raise SystemExit(f"Failed to render {url}: status {response.status_code}")
            output = HTML_DIR / f"{name}.html"
            output.write_text(response.content.decode("utf-8", errors="replace"), encoding="utf-8")
            print(f"Rendered: {output}")


if __name__ == "__main__":
    main()
