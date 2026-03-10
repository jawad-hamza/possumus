from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Account, AuditLog, BackupSnapshot, PartyLedgerEntry, Voucher
from accounts.services.settlements import create_payment
from pos.models import Brand, Category, CompanyProfile, CustomRole, Customer, Product, Purchase, PurchaseItem, Sale, SaleItem, Supplier, SystemSettings, UserSessionLog
from pos.services.access import ADMIN_ROLE, CASHIER_ROLE, assign_role, ensure_role_groups
from pos.test_support import AuthenticatedAdminTestCase


class PosFlowTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        accounts = [
            ("Cash", Account.ASSET),
            ("Bank", Account.ASSET),
            ("Accounts Receivable", Account.ASSET),
            ("Accounts Payable", Account.LIABILITY),
            ("Sales Tax Payable", Account.LIABILITY),
            ("Inventory", Account.ASSET),
            ("Sales", Account.INCOME),
            ("Cost of Goods Sold", Account.EXPENSE),
            ("Purchase", Account.EXPENSE),
        ]
        for title, acc_type in accounts:
            Account.objects.create(title=title, type=acc_type)

    def _set_cart(self, product, quantity=1):
        session = self.client.session
        session["cart"] = {
            str(product.id): {
                "name": product.name,
                "price": str(product.price),
                "original_price": str(product.price),
                "discount_percent": "0.00",
                "quantity": quantity,
            }
        }
        session.save()

    def _purchase_formset_payload(self, product, *, quantity, cost_price, sale_price, total_forms=3):
        payload = {
            "items-TOTAL_FORMS": str(total_forms),
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-product": str(product.id),
            "items-0-quantity": str(quantity),
            "items-0-cost_price": str(cost_price),
            "items-0-sale_price": str(sale_price),
        }
        for index in range(1, total_forms):
            payload[f"items-{index}-product"] = ""
            payload[f"items-{index}-quantity"] = ""
            payload[f"items-{index}-cost_price"] = ""
            payload[f"items-{index}-sale_price"] = ""
        return payload

    def test_credit_checkout_posts_ar_sales_and_inventory_entries(self):
        customer = Customer.objects.create(name="Ali")
        product = Product.objects.create(
            name="Soap",
            sku="SOAP-1",
            cost_price=Decimal("60.00"),
            price=Decimal("100.00"),
            stock=5,
        )
        self._set_cart(product, quantity=1)

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "checkout",
                "discount_rate": "0",
                "tax_rate": "0",
                "is_credit": "on",
                "customer_id": customer.id,
                "payment_method": "cash",
            },
        )

        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)
        self.assertEqual(voucher.voucher_type, Voucher.SALE)
        self.assertTrue(
            voucher.lines.filter(account__title="Accounts Receivable", debit=Decimal("100.00")).exists()
        )
        self.assertTrue(voucher.lines.filter(account__title="Sales", credit=Decimal("100.00")).exists())
        self.assertTrue(
            voucher.lines.filter(account__title="Cost of Goods Sold", debit=Decimal("60.00")).exists()
        )
        self.assertTrue(voucher.lines.filter(account__title="Inventory", credit=Decimal("60.00")).exists())
        self.assertTrue(
            PartyLedgerEntry.objects.filter(voucher=voucher, object_id=customer.id, debit=Decimal("100.00")).exists()
        )

    def test_cash_checkout_uses_selected_payment_method(self):
        product = Product.objects.create(
            name="Tea",
            sku="TEA-1",
            cost_price=Decimal("20.00"),
            price=Decimal("50.00"),
            stock=5,
        )
        self._set_cart(product, quantity=2)

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "checkout",
                "discount_rate": "0",
                "tax_rate": "0",
                "payment_method": "bank",
            },
        )

        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)
        self.assertEqual(sale.paid_amount, Decimal("100.00"))
        self.assertTrue(voucher.lines.filter(account__title="Bank", debit=Decimal("100.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Inventory", credit=Decimal("40.00")).exists())

    def test_checkout_rolls_back_if_accounting_post_fails(self):
        product = Product.objects.create(
            name="Roll Back",
            sku="RB-1",
            cost_price=Decimal("10.00"),
            price=Decimal("25.00"),
            stock=5,
        )
        self._set_cart(product, quantity=2)

        with mock.patch("pos.views.post_sale_from_pos", side_effect=ValueError("posting failed")):
            with self.assertRaisesMessage(ValueError, "posting failed"):
                self.client.post(
                    reverse("pos_screen"),
                    {
                        "action": "checkout",
                        "discount_rate": "0",
                        "tax_rate": "0",
                        "payment_method": "cash",
                    },
                )

        product.refresh_from_db()
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Voucher.objects.count(), 0)
        self.assertEqual(product.stock, 5)

    def test_purchase_add_updates_inventory_pricing_and_credit_voucher(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(
            name="Rice",
            sku="RICE-1",
            cost_price=Decimal("10.00"),
            price=Decimal("50.00"),
            stock=1,
            minimum_margin_percent=Decimal("10.00"),
        )

        payload = {
            "supplier": supplier.id,
            "payment_method": "cash",
            "paid_amount": "0.00",
        }
        payload.update(self._purchase_formset_payload(product, quantity=3, cost_price="20.00", sale_price="30.00"))

        response = self.client.post(reverse("purchase_add"), payload)

        self.assertEqual(response.status_code, 302)
        purchase = Purchase.objects.get()
        product.refresh_from_db()
        voucher = Voucher.objects.get(ref_model="Purchase", ref_id=purchase.id)

        self.assertEqual(purchase.total_amount, Decimal("60.00"))
        self.assertEqual(product.stock, 4)
        self.assertEqual(product.cost_price, Decimal("20.00"))
        self.assertEqual(product.price, Decimal("30.00"))
        self.assertTrue(voucher.lines.filter(account__title="Inventory", debit=Decimal("60.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Accounts Payable", credit=Decimal("60.00")).exists())
        self.assertTrue(
            PartyLedgerEntry.objects.filter(voucher=voucher, object_id=supplier.id, credit=Decimal("60.00")).exists()
        )

    def test_purchase_rejects_sale_price_below_minimum_margin(self):
        supplier = Supplier.objects.create(name="Margin Supplier")
        product = Product.objects.create(
            name="Margin Product",
            sku="M-1",
            cost_price=Decimal("80.00"),
            price=Decimal("100.00"),
            stock=1,
            minimum_margin_percent=Decimal("25.00"),
        )

        payload = {
            "supplier": supplier.id,
            "payment_method": "cash",
            "paid_amount": "0.00",
        }
        payload.update(self._purchase_formset_payload(product, quantity=1, cost_price="100.00", sale_price="110.00"))

        response = self.client.post(reverse("purchase_add"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertContains(response, "Sale price must be at least 125.00")

    def test_purchase_pages_show_later_supplier_settlement(self):
        supplier = Supplier.objects.create(name="Settled Supplier")
        product = Product.objects.create(
            name="Settled Product",
            sku="SET-1",
            cost_price=Decimal("20.00"),
            price=Decimal("35.00"),
            stock=5,
        )
        purchase = Purchase.objects.create(supplier=supplier, payment_method="cash", paid_amount=Decimal("0.00"))
        PurchaseItem.objects.create(
            purchase=purchase,
            product=product,
            quantity=2,
            cost_price=Decimal("20.00"),
            sale_price=Decimal("35.00"),
        )
        purchase.refresh_from_db()

        create_payment(party=supplier, amount=Decimal("40.00"), payment_method="cash")

        list_response = self.client.get(reverse("purchase_list"))

        self.assertEqual(list_response.status_code, 200)
        listed_purchase = next(row for row in list_response.context["purchases"] if row.id == purchase.id)
        self.assertEqual(listed_purchase.report_paid_amount, Decimal("40.00"))
        self.assertEqual(listed_purchase.report_balance_due, Decimal("0.00"))
        self.assertEqual(listed_purchase.report_payment_status, "paid")
        self.assertContains(list_response, "Later settlement Rs 40.00")

        detail_response = self.client.get(reverse("purchase_detail", args=[purchase.id]))

        self.assertEqual(detail_response.status_code, 200)
        detail_purchase = detail_response.context["purchase"]
        self.assertEqual(detail_purchase.report_paid_amount, Decimal("40.00"))
        self.assertEqual(detail_purchase.report_balance_due, Decimal("0.00"))
        self.assertContains(detail_response, "Later supplier payments Rs 40.00")

    def test_purchase_item_switches_stock_between_products(self):
        supplier = Supplier.objects.create(name="Switch Supplier")
        product_a = Product.objects.create(name="A", sku="A-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=10)
        product_b = Product.objects.create(name="B", sku="B-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=5)
        purchase = Purchase.objects.create(supplier=supplier)
        item = PurchaseItem.objects.create(
            purchase=purchase,
            product=product_a,
            quantity=3,
            cost_price=Decimal("10.00"),
            sale_price=Decimal("15.00"),
        )

        item.product = product_b
        item.quantity = 2
        item.cost_price = Decimal("10.00")
        item.sale_price = Decimal("15.00")
        item.save()

        product_a.refresh_from_db()
        product_b.refresh_from_db()
        purchase.refresh_from_db()

        self.assertEqual(product_a.stock, 10)
        self.assertEqual(product_b.stock, 7)
        self.assertEqual(purchase.total_amount, Decimal("20.00"))

    def test_daily_sales_report_uses_item_subtotal(self):
        product = Product.objects.create(
            name="Report",
            sku="REP-1",
            cost_price=Decimal("20.00"),
            price=Decimal("50.00"),
            stock=10,
        )
        sale = Sale.objects.create(discount_rate=Decimal("10.00"), tax_rate=Decimal("10.00"), payment_method="cash")
        SaleItem.objects.create(sale=sale, product=product, quantity=2, price=Decimal("50.00"), cost_price=Decimal("20.00"))
        sale.refresh_from_db()

        response = self.client.get(reverse("daily_sales_report"))

        self.assertEqual(response.status_code, 200)
        report_sale = next(row for row in response.context["sales"] if row.id == sale.id)
        self.assertEqual(report_sale.report_subtotal, Decimal("100.00"))
        self.assertEqual(report_sale.report_discount_amount, Decimal("10.00"))
        self.assertEqual(report_sale.report_tax_amount, Decimal("9.00"))
        self.assertEqual(response.context["totals"]["total"], Decimal("99.00"))
        self.assertEqual(response.context["totals"]["cost"], Decimal("40.00"))
        self.assertEqual(response.context["totals"]["gross_profit"], Decimal("50.00"))

    def test_scan_barcode_accepts_barcode_value(self):
        product = Product.objects.create(
            name="Barcode Product",
            sku="BC-1",
            barcode="1234567890",
            cost_price=Decimal("5.00"),
            price=Decimal("9.00"),
            stock=4,
        )

        response = self.client.get(reverse("scan_barcode", args=[product.barcode]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        session = self.client.session
        self.assertEqual(session["cart"][str(product.id)]["quantity"], 1)

    def test_add_customer_flow_returns_to_pos_with_credit_selected(self):
        next_url = f"{reverse('pos_screen')}?credit=1"
        response = self.client.post(
            f"{reverse('customer_add')}?next={quote(next_url, safe='/?=&')}",
            {
                "name": "POS Customer",
                "phone": "03001234567",
                "email": "pos@example.com",
                "address": "Market Road",
                "next": next_url,
            },
        )

        customer = Customer.objects.get(name="POS Customer")
        self.assertTrue(customer.is_active)
        expected_url = f"{next_url}&customer_id={customer.id}"
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, expected_url)

        pos_response = self.client.get(expected_url)
        self.assertEqual(pos_response.status_code, 200)
        self.assertEqual(pos_response.context["selected_customer_id"], str(customer.id))
        self.assertTrue(pos_response.context["prefill_credit_sale"])

    def test_pos_rejects_discounted_price_below_minimum_margin(self):
        product = Product.objects.create(
            name="Margin Guard",
            sku="MG-1",
            cost_price=Decimal("80.00"),
            price=Decimal("120.00"),
            stock=5,
            minimum_margin_percent=Decimal("25.00"),
        )
        self._set_cart(product, quantity=1)

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "update_line",
                "product_id": product.id,
                "custom_price": "100.00",
                "line_discount": "10.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertEqual(session["cart"][str(product.id)]["price"], "120.00")
        self.assertEqual(session["cart"][str(product.id)]["discount_percent"], "0.00")

    def test_checkout_persists_custom_price_and_line_discount(self):
        product = Product.objects.create(
            name="Negotiated Item",
            sku="NEG-1",
            cost_price=Decimal("50.00"),
            price=Decimal("100.00"),
            stock=5,
            minimum_margin_percent=Decimal("10.00"),
        )
        self._set_cart(product, quantity=1)

        update_response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "update_line",
                "product_id": product.id,
                "custom_price": "95.00",
                "line_discount": "5.00",
            },
        )
        self.assertEqual(update_response.status_code, 302)

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "checkout",
                "discount_rate": "0",
                "tax_rate": "0",
                "payment_method": "cash",
            },
        )

        self.assertEqual(response.status_code, 302)
        sale_item = SaleItem.objects.get()
        sale = sale_item.sale
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)

        self.assertEqual(sale_item.original_price, Decimal("100.00"))
        self.assertEqual(sale_item.price, Decimal("95.00"))
        self.assertEqual(sale_item.discount_percent, Decimal("5.00"))
        self.assertEqual(sale_item.discounted_unit_price, Decimal("90.25"))
        self.assertEqual(sale.total_amount, Decimal("90.25"))
        self.assertTrue(voucher.lines.filter(account__title="Sales", credit=Decimal("90.25")).exists())

    def test_product_list_filters_and_sorts(self):
        supplier = Supplier.objects.create(name="Filter Supplier")
        grocery = Category.objects.create(name="Grocery")
        alpha = Brand.objects.create(name="Alpha")
        beta = Brand.objects.create(name="Beta")
        lower_price = Product.objects.create(
            name="Lower",
            sku="LOW-1",
            brand=alpha,
            category=grocery,
            supplier=supplier,
            cost_price=Decimal("10.00"),
            price=Decimal("30.00"),
            stock=2,
        )
        higher_price = Product.objects.create(
            name="Higher",
            sku="HIGH-1",
            brand=alpha,
            category=grocery,
            supplier=supplier,
            cost_price=Decimal("10.00"),
            price=Decimal("45.00"),
            stock=4,
        )
        Product.objects.create(
            name="Other Brand",
            sku="OTH-1",
            brand=beta,
            category=grocery,
            supplier=supplier,
            cost_price=Decimal("10.00"),
            price=Decimal("60.00"),
            stock=5,
        )

        response = self.client.get(
            reverse("product_list"),
            {"brand": alpha.id, "category": grocery.id, "sort": "price_high"},
        )

        self.assertEqual(response.status_code, 200)
        products = list(response.context["products"].object_list)
        self.assertEqual([product.id for product in products], [higher_price.id, lower_price.id])
        self.assertEqual(response.context["summary_total"], 2)

    def test_quick_create_product_endpoint_creates_catalogue_item(self):
        brand = Brand.objects.create(name="Quick Brand")
        category = Category.objects.create(name="Quick Category")
        supplier = Supplier.objects.create(name="Quick Supplier")

        response = self.client.post(
            reverse("quick_create_product"),
            {
                "quick_product-name": "Quick Product",
                "quick_product-sku": "QP-1",
                "quick_product-barcode": "",
                "quick_product-brand": str(brand.id),
                "quick_product-category": str(category.id),
                "quick_product-supplier": str(supplier.id),
                "quick_product-price": "120.00",
                "quick_product-minimum_margin_percent": "15.00",
                "quick_product-low_stock_threshold": "7",
                "quick_product-location": "Front Rack",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        product = Product.objects.get(id=payload["product"]["id"])
        self.assertEqual(product.name, "Quick Product")
        self.assertEqual(product.stock, 0)
        self.assertEqual(product.cost_price, Decimal("0.00"))
        self.assertEqual(product.price, Decimal("120.00"))
        self.assertEqual(product.brand, brand)
        self.assertEqual(product.category, category)
        self.assertEqual(product.supplier, supplier)

    def test_invoice_receipt_uses_thermal_layout(self):
        customer = Customer.objects.create(name="Receipt Customer")
        product = Product.objects.create(
            name="Receipt Product",
            sku="REC-1",
            cost_price=Decimal("15.00"),
            price=Decimal("25.00"),
            stock=5,
        )
        sale = Sale.objects.create(customer=customer, payment_method="cash", paid_amount=Decimal("25.00"))
        SaleItem.objects.create(sale=sale, product=product, quantity=1, price=Decimal("25.00"), cost_price=Decimal("15.00"))

        response = self.client.get(reverse("invoice", args=[sale.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Print 50mm")
        self.assertContains(response, "size: 50mm auto")
    def test_main_routes_render(self):
        customer = Customer.objects.create(name="Route Customer")
        supplier = Supplier.objects.create(name="Route Supplier")
        product = Product.objects.create(name="Route Product", sku="ROUTE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=5)
        purchase = Purchase.objects.create(supplier=supplier, payment_method="cash", paid_amount=Decimal("0.00"))
        PurchaseItem.objects.create(purchase=purchase, product=product, quantity=1, cost_price=Decimal("10.00"), sale_price=Decimal("15.00"))
        sale = Sale.objects.create(customer=customer, payment_method="cash", paid_amount=Decimal("0.00"))
        SaleItem.objects.create(sale=sale, product=product, quantity=1, price=Decimal("15.00"), cost_price=Decimal("10.00"))

        self.assertRedirects(self.client.get(reverse("home")), reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("product_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("pos_screen")).status_code, 200)
        self.assertEqual(self.client.get(reverse("purchase_add")).status_code, 200)
        self.assertEqual(self.client.get(reverse("purchase_edit", args=[purchase.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("customer_add")).status_code, 200)
        self.assertEqual(self.client.get(reverse("supplier_add")).status_code, 200)
        self.assertEqual(self.client.get(reverse("customer_detail", args=[customer.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("supplier_detail", args=[supplier.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("purchase_detail", args=[purchase.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("party_statement_page")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts_home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("trial_balance")).status_code, 200)
        self.assertEqual(self.client.get(reverse("profit_loss")).status_code, 200)
        self.assertEqual(self.client.get(reverse("balance_sheet")).status_code, 200)
        self.assertEqual(self.client.get(reverse("reports_home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("daily_sales_report")).status_code, 200)
        self.assertEqual(self.client.get(reverse("invoice", args=[sale.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("invoice_pdf", args=[sale.id])).status_code, 200)













    def test_settings_page_updates_branding_and_tax_rules(self):
        branding_response = self.client.post(
            reverse("settings_page"),
            {
                "action": "save_branding",
                "branding-name": "Blue Mart",
                "branding-phone": "03001234567",
                "branding-email": "hello@bluemart.test",
                "branding-address": "Main Market",
                "branding-tax_registration_number": "GST-7788",
                "branding-invoice_footer": "Visit again.",
                "branding-qr_note": "Pay by QR",
                "branding-show_qr": "on",
            },
        )
        self.assertEqual(branding_response.status_code, 302)

        settings_response = self.client.post(
            reverse("settings_page"),
            {
                "action": "save_rules",
                "business-gst_enabled": "on",
                "business-tax_mode": SystemSettings.CATEGORY_GST,
                "business-tax_label": "GST",
                "business-allow_credit_sales": "on",
                "business-allow_price_override": "on",
                "business-allow_line_discount": "on",
                "business-default_sale_payment_method": "bank",
                "business-thermal_printer_width_mm": "58",
            },
        )
        self.assertEqual(settings_response.status_code, 302)

        company = CompanyProfile.objects.get(is_active=True)
        settings_obj = SystemSettings.load()
        self.assertEqual(company.name, "Blue Mart")
        self.assertEqual(company.tax_registration_number, "GST-7788")
        self.assertEqual(settings_obj.tax_mode, SystemSettings.CATEGORY_GST)
        self.assertEqual(settings_obj.default_sale_payment_method, "bank")
        self.assertEqual(settings_obj.thermal_printer_width_mm, 58)

    def test_settings_page_limits_audit_trail_to_ten_entries_by_default(self):
        for number in range(1, 15):
            AuditLog.objects.create(
                action=f"event_{number}",
                actor=self.admin_user,
                target_model="SystemSettings",
                target_id="1",
                description=f"Event {number}",
            )

        response = self.client.get(reverse("settings_page"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["audit_mode"], "page")
        self.assertEqual(len(response.context["audit_logs"]), 10)
        self.assertEqual(response.context["audit_page_obj"].number, 1)
        self.assertEqual(response.context["audit_event_count"], 14)
        self.assertEqual(
            [log.description for log in response.context["audit_logs"]],
            [f"Event {number}" for number in range(14, 4, -1)],
        )
        self.assertContains(response, "Show All")
        self.assertContains(response, "Next")

        page_two_response = self.client.get(reverse("settings_page"), {"audit_page": 2})

        self.assertEqual(page_two_response.status_code, 200)
        self.assertEqual(page_two_response.context["audit_mode"], "page")
        self.assertEqual(page_two_response.context["audit_page_obj"].number, 2)
        self.assertEqual(
            [log.description for log in page_two_response.context["audit_logs"]],
            [f"Event {number}" for number in range(4, 0, -1)],
        )
        self.assertContains(page_two_response, "Previous")

        show_all_response = self.client.get(reverse("settings_page"), {"audit_mode": "all"})

        self.assertEqual(show_all_response.status_code, 200)
        self.assertEqual(show_all_response.context["audit_mode"], "all")
        self.assertIsNone(show_all_response.context["audit_page_obj"])
        self.assertEqual(len(show_all_response.context["audit_logs"]), 14)
        self.assertContains(show_all_response, "Back to Top 10")

    def test_settings_page_groups_sections_into_tabs(self):
        response = self.client.get(reverse("settings_page"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="settings-business-tab"')
        self.assertContains(response, 'id="settings-banking-tab"')
        self.assertContains(response, 'id="settings-backups-tab"')
        self.assertContains(response, 'id="settings-audit-tab"')
        self.assertContains(response, 'id="settings-team-tab"')
        self.assertContains(response, 'id="settings-business-pane"')
        self.assertContains(response, 'id="settings-banking-pane"')
        self.assertContains(response, 'id="settings-backups-pane"')
        self.assertContains(response, 'id="settings-audit-pane"')
        self.assertContains(response, 'id="settings-team-pane"')

    def test_settings_page_can_upload_database_snapshot(self):
        uploaded_file = SimpleUploadedFile(
            "staging-snapshot.sqlite3",
            b"sqlite backup content",
            content_type="application/octet-stream",
        )

        with TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir)):
                response = self.client.post(
                    reverse("settings_page"),
                    {
                        "action": "upload_backup",
                        "upload_backup_label": "Staging Snapshot",
                        "upload_backup_notes": "Uploaded during recovery drill",
                        "backup_upload_file": uploaded_file,
                    },
                )

                self.assertEqual(response.status_code, 200)
                snapshot = BackupSnapshot.objects.get()
                self.assertEqual(snapshot.label, "Staging Snapshot")
                self.assertEqual(snapshot.notes, "Uploaded during recovery drill")
                self.assertEqual(snapshot.created_by, self.admin_user)
                self.assertTrue(snapshot.file_name.endswith(".sqlite3"))

                stored_path = Path(tmpdir) / "backups" / snapshot.file_name
                self.assertTrue(stored_path.exists())
                self.assertEqual(stored_path.read_bytes(), b"sqlite backup content")
                self.assertTrue(AuditLog.objects.filter(action="backup_uploaded", target_id=str(snapshot.id)).exists())
                self.assertContains(response, "Staging Snapshot")

    def test_category_gst_checkout_posts_tax_to_liability(self):
        settings_obj = SystemSettings.load()
        settings_obj.gst_enabled = True
        settings_obj.tax_mode = SystemSettings.CATEGORY_GST
        settings_obj.tax_label = "GST"
        settings_obj.save()

        taxable_category = Category.objects.create(
            name="Electronics",
            tax_status=Category.TAXABLE,
            gst_rate=Decimal("18.00"),
        )
        product = Product.objects.create(
            name="Speaker",
            sku="SP-1",
            category=taxable_category,
            cost_price=Decimal("100.00"),
            price=Decimal("200.00"),
            stock=3,
        )
        self._set_cart(product, quantity=1)

        pos_response = self.client.get(reverse("pos_screen"))
        self.assertFalse(pos_response.context["manual_tax_enabled"])
        self.assertTrue(pos_response.context["uses_category_gst"])

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "checkout",
                "discount_rate": "10",
                "tax_rate": "99",
                "payment_method": "cash",
            },
        )

        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)

        self.assertEqual(sale.tax_mode, Sale.TAX_MODE_CATEGORY)
        self.assertEqual(sale.tax_rate, Decimal("0.00"))
        self.assertEqual(sale.tax_amount, Decimal("32.40"))
        self.assertEqual(sale.total_amount, Decimal("212.40"))
        self.assertTrue(voucher.lines.filter(account__title="Sales", credit=Decimal("180.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Sales Tax Payable", credit=Decimal("32.40")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Cash", debit=Decimal("212.40")).exists())

    def test_settings_can_disable_price_override_and_line_discount(self):
        settings_obj = SystemSettings.load()
        settings_obj.allow_price_override = False
        settings_obj.allow_line_discount = False
        settings_obj.save()

        product = Product.objects.create(
            name="Locked Price",
            sku="LOCK-1",
            cost_price=Decimal("50.00"),
            price=Decimal("100.00"),
            stock=5,
        )
        self._set_cart(product, quantity=1)

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "update_line",
                "product_id": product.id,
                "custom_price": "90.00",
                "line_discount": "5.00",
            },
        )

        self.assertEqual(response.status_code, 302)
        session = self.client.session
        self.assertEqual(session["cart"][str(product.id)]["price"], "100.00")
        self.assertEqual(session["cart"][str(product.id)]["discount_percent"], "0.00")

    def test_checkout_reapplies_locked_price_rules_from_settings(self):
        settings_obj = SystemSettings.load()
        settings_obj.allow_price_override = False
        settings_obj.allow_line_discount = False
        settings_obj.save()

        product = Product.objects.create(
            name="Locked Checkout",
            sku="LOCK-CHECK-1",
            cost_price=Decimal("50.00"),
            price=Decimal("100.00"),
            stock=5,
        )
        session = self.client.session
        session["cart"] = {
            str(product.id): {
                "name": product.name,
                "price": "90.00",
                "original_price": "100.00",
                "discount_percent": "5.00",
                "quantity": 1,
            }
        }
        session.save()

        response = self.client.post(
            reverse("pos_screen"),
            {
                "action": "checkout",
                "discount_rate": "0",
                "tax_rate": "0",
                "payment_method": "cash",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        sale_item = SaleItem.objects.get()
        sale = sale_item.sale
        self.assertEqual(sale_item.original_price, Decimal("100.00"))
        self.assertEqual(sale_item.price, Decimal("100.00"))
        self.assertEqual(sale_item.discount_percent, Decimal("0.00"))
        self.assertEqual(sale.total_amount, Decimal("100.00"))
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_expiry_tracking_toggle_controls_product_ui_and_dashboard_kpi(self):
        product = Product.objects.create(
            name="Milk",
            sku="MILK-1",
            cost_price=Decimal("100.00"),
            price=Decimal("140.00"),
            stock=8,
        )
        settings_obj = SystemSettings.load()
        settings_obj.enable_product_expiry = False
        settings_obj.save()

        product_edit_response = self.client.get(reverse("product_edit", args=[product.id]))
        product_list_response = self.client.get(reverse("product_list"))
        dashboard_response = self.client.get(reverse("dashboard"))

        self.assertContains(product_edit_response, "Expiry Date")
        self.assertNotContains(product_list_response, "<th>Expiry</th>", html=False)
        self.assertNotIn("Expiry Alerts", [item["title"] for item in dashboard_response.context["dashboard_kpis"]])

        settings_obj.enable_product_expiry = True
        settings_obj.expiry_alert_days = 7
        settings_obj.save()

        edit_response = self.client.post(
            reverse("product_edit", args=[product.id]),
            {
                "name": product.name,
                "brand": "",
                "sku": product.sku,
                "barcode": "",
                "category": "",
                "supplier": "",
                "cost_price": "100.00",
                "price": "140.00",
                "minimum_margin_percent": "10.00",
                "stock": "8",
                "low_stock_threshold": "2",
                "location": "Rack 2",
                "expiry_date": "2026-03-15",
                "is_active": "on",
            },
        )
        self.assertEqual(edit_response.status_code, 302)

        product.refresh_from_db()
        self.assertEqual(str(product.expiry_date), "2026-03-15")

        product_edit_response = self.client.get(reverse("product_edit", args=[product.id]))
        product_list_response = self.client.get(reverse("product_list"))
        dashboard_response = self.client.get(reverse("dashboard"))

        self.assertContains(product_edit_response, "Expiry Date")
        self.assertContains(product_list_response, "<th>Expiry</th>", html=False)
        self.assertContains(product_list_response, "15 Mar 2026")
        self.assertIn("Expiry Alerts", [item["title"] for item in dashboard_response.context["dashboard_kpis"]])
    def test_daily_sales_report_uses_configured_tax_label(self):
        settings_obj = SystemSettings.load()
        settings_obj.tax_label = "VAT"
        settings_obj.save()

        response = self.client.get(reverse("daily_sales_report"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tax_label"], "VAT")
        self.assertContains(response, "VAT")


class CustomRoleSettingsTests(AuthenticatedAdminTestCase):
    def test_settings_can_create_custom_role_and_reassign_existing_user(self):
        role_payload = {
            "action": "create_role",
            "role-name": "Manager",
            "role-description": "Can monitor the business and work on products.",
            "role-perm_dashboard": "view",
            "role-perm_pos": "none",
            "role-perm_products": "manage",
            "role-perm_stock_control": "view",
            "role-perm_purchases": "view",
            "role-perm_expenses": "none",
            "role-perm_parties": "view",
            "role-perm_accounts": "none",
            "role-perm_reports": "view",
            "role-perm_settings": "none",
        }
        response = self.client.post(reverse("settings_page"), role_payload)
        self.assertEqual(response.status_code, 302)

        role = CustomRole.objects.get(name="Manager")
        self.assertEqual(role.permissions["products"], "manage")
        self.assertEqual(role.permissions["dashboard"], "view")

        user_model = get_user_model()
        member = user_model.objects.create_user(username="manager1", password="ManagerPass123!")
        assign_response = self.client.post(
            reverse("settings_page"),
            {
                "action": "change_user_role",
                "user_id": member.id,
                "role": f"custom:{role.slug}",
            },
        )
        self.assertEqual(assign_response.status_code, 302)
        self.assertTrue(member.groups.filter(name=role.group_name).exists())


class RoleAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_role_groups()
        user_model = get_user_model()
        cls.admin_user = user_model.objects.create_user(
            username="owner",
            password="OwnerPass123!",
            is_staff=True,
        )
        assign_role(cls.admin_user, ADMIN_ROLE)
        cls.cashier_user = user_model.objects.create_user(
            username="cashier1",
            password="CashierPass123!",
        )
        assign_role(cls.cashier_user, CASHIER_ROLE)

        accounts = [
            ("Cash", Account.ASSET),
            ("Bank", Account.ASSET),
            ("Accounts Receivable", Account.ASSET),
            ("Accounts Payable", Account.LIABILITY),
            ("Sales Tax Payable", Account.LIABILITY),
            ("Inventory", Account.ASSET),
            ("Sales", Account.INCOME),
            ("Cost of Goods Sold", Account.EXPENSE),
            ("Purchase", Account.EXPENSE),
        ]
        for title, acc_type in accounts:
            Account.objects.get_or_create(title=title, defaults={"type": acc_type})

    def _set_cart(self, product, quantity=1):
        session = self.client.session
        session["cart"] = {
            str(product.id): {
                "name": product.name,
                "price": str(product.price),
                "original_price": str(product.price),
                "discount_percent": "0.00",
                "quantity": quantity,
            }
        }
        session.save()

    def test_cashier_login_and_logout_are_logged(self):
        response = self.client.post(
            reverse("login"),
            {"username": "cashier1", "password": "CashierPass123!"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("pos_screen"))
        session_log = UserSessionLog.objects.get(user=self.cashier_user)
        self.assertIsNone(session_log.logout_at)

        logout_response = self.client.post(reverse("logout"))
        self.assertEqual(logout_response.status_code, 302)
        session_log.refresh_from_db()
        self.assertIsNotNone(session_log.logout_at)

    def test_cashier_is_redirected_from_admin_pages(self):
        self.client.force_login(self.cashier_user)

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("pos_screen"))


    def test_custom_role_can_view_allowed_pages_but_manage_routes_are_blocked(self):
        role = CustomRole.objects.create(
            name="Viewer Manager",
            description="Can open dashboard and product pages but cannot edit.",
            permissions={
                "dashboard": "view",
                "pos": "none",
                "products": "view",
                "stock_control": "none",
                "purchases": "none",
                "expenses": "none",
                "parties": "none",
                "accounts": "none",
                "reports": "view",
                "settings": "none",
            },
        )
        user_model = get_user_model()
        user = user_model.objects.create_user(username="viewer1", password="ViewerPass123!")
        assign_role(user, f"custom:{role.slug}")

        self.client.force_login(user)

        home_response = self.client.get(reverse("home"))
        self.assertEqual(home_response.status_code, 302)
        self.assertEqual(home_response.url, reverse("dashboard"))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("product_list")).status_code, 200)

        blocked_response = self.client.get(reverse("product_add"))
        self.assertEqual(blocked_response.status_code, 302)
        self.assertEqual(blocked_response.url, reverse("dashboard"))
















