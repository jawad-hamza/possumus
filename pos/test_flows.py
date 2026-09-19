"""POS, purchase and catalog flows: failures must come back as readable messages, never error pages."""
import csv
import io
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from accounts.models import AuditLog, Voucher
from pos.models import (
    Brand,
    Category,
    Customer,
    Product,
    Purchase,
    PurchaseItem,
    Sale,
    SaleItem,
    StockAdjustment,
    Supplier,
)
from pos.services.access import CASHIER_ROLE, assign_role
from pos.services.cart_pricing import stock_shortfall, tracks_stock
from pos.services.catalog_io import COLUMNS, export_catalog_csv_text, parse_catalog
from pos.services.returns import create_purchase_return
from pos.test_support import AuthenticatedAdminTestCase


def _cart_line(product, quantity="1"):
    return {
        "name": product.name,
        "price": str(product.price),
        "original_price": str(product.price),
        "discount_percent": "0.00",
        "quantity": str(quantity),
    }


class PosTestCase(AuthenticatedAdminTestCase):
    def _set_cart(self, *lines):
        session = self.client.session
        session["cart"] = {str(product.id): _cart_line(product, quantity) for product, quantity in lines}
        session.save()

    def _product(self, sku="TEA-1", **overrides):
        values = {
            "name": f"Product {sku}",
            "sku": sku,
            "cost_price": Decimal("10.00"),
            "price": Decimal("25.00"),
            "stock": 5,
        }
        values.update(overrides)
        return Product.objects.create(**values)

    def _messages(self, response):
        return [str(message) for message in response.context["messages"]]


class CheckoutFailureTests(PosTestCase):
    def test_ledger_failure_rolls_back_and_tells_the_cashier(self):
        product = self._product()
        self._set_cart((product, 2))

        with mock.patch("pos.views.pos_views.post_sale_from_pos", side_effect=ValueError("Bank ledger is closed")):
            response = self.client.post(
                reverse("pos_screen"),
                {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("Bank ledger is closed" in text for text in self._messages(response)))
        product.refresh_from_db()
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(SaleItem.objects.count(), 0)
        self.assertEqual(Voucher.objects.count(), 0)
        self.assertEqual(product.stock, 5)
        # The cart survives so the cashier can retry instead of re-scanning everything.
        self.assertIn(str(product.id), self.client.session["cart"])

    def test_full_discount_sale_is_refused_cleanly(self):
        product = self._product(minimum_margin_percent=Decimal("0"))
        self._set_cart((product, 1))

        response = self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "discount_rate": "100", "tax_rate": "0", "payment_method": "cash"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("adds up to zero" in text for text in self._messages(response)))
        product.refresh_from_db()
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Voucher.objects.count(), 0)
        self.assertEqual(product.stock, 5)

    def test_stock_taken_by_another_till_mid_checkout_is_a_message(self):
        product = self._product(stock=1)
        self._set_cart((product, 1))
        original_create = SaleItem.objects.create

        def create_after_rival_sale(**kwargs):
            Product.objects.filter(pk=product.pk).update(stock=0)
            kwargs["product"].stock = Decimal("0")
            return original_create(**kwargs)

        with mock.patch("pos.views.pos_views.SaleItem.objects.create", side_effect=create_after_rival_sale):
            response = self.client.post(
                reverse("pos_screen"),
                {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertTrue(any("Sale not saved" in text for text in self._messages(response)))


class ScanBarcodeTests(PosTestCase):
    def test_get_cannot_change_the_cart(self):
        product = self._product(barcode="12345")

        response = self.client.get(reverse("scan_barcode", args=[product.barcode]))

        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.client.session.get("cart", {}), {})

    def test_post_keeps_the_json_contract(self):
        product = self._product(barcode="12345", stock=1)
        url = reverse("scan_barcode", args=[product.barcode])

        first = self.client.post(url)
        second = self.client.post(url)
        missing = self.client.post(reverse("scan_barcode", args=["NOPE"]))

        self.assertEqual(first.json(), {"status": "ok", "product_id": product.id})
        self.assertEqual(second.json(), {"status": "out_of_stock", "product": product.name})
        self.assertEqual(missing.json(), {"status": "notfound"})
        self.assertEqual(self.client.session["cart"][str(product.id)]["quantity"], "1")

    def test_post_requires_a_csrf_token(self):
        product = self._product(barcode="12345")
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.admin_user)

        response = strict_client.post(reverse("scan_barcode", args=[product.barcode]))

        self.assertEqual(response.status_code, 403)

    def test_pos_page_scans_with_post_and_csrf_header(self):
        response = self.client.get(reverse("pos_screen"))

        self.assertContains(response, "method: 'POST'")
        self.assertContains(response, "X-CSRFToken")


class HeldCartTests(PosTestCase):
    def test_hold_parks_the_cart_and_resume_brings_it_back_with_its_customer(self):
        customer = Customer.objects.create(name="Ayesha")
        product = self._product(unit="kg")
        self._set_cart((product, "0.5"))

        hold = self.client.post(
            reverse("pos_screen"),
            {
                "action": "hold_cart",
                "selected_customer_id": customer.id,
                "preserve_payment_method": "cash",
                "preserve_discount_rate": "5",
            },
        )

        self.assertEqual(hold.status_code, 302)
        self.assertEqual(hold.url, reverse("pos_screen"))
        self.assertEqual(self.client.session["cart"], {})
        page = self.client.get(reverse("pos_screen"))
        held = page.context["held_carts"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["label"], "Ayesha")
        self.assertEqual(held[0]["line_count"], 1)
        self.assertEqual(held[0]["total"], Decimal("12.50"))
        self.assertIsNotNone(held[0]["held_at"])
        self.assertContains(page, "Ayesha")

        resume = self.client.post(reverse("pos_screen"), {"action": "resume_cart", "held_id": held[0]["id"]})

        self.assertEqual(resume.status_code, 302)
        self.assertIn(f"customer_id={customer.id}", resume.url)
        self.assertIn("discount_rate=5", resume.url)
        self.assertEqual(self.client.session["cart"][str(product.id)]["quantity"], "0.5")
        self.assertEqual(self.client.session["held_carts"], [])

    def test_resuming_with_a_sale_on_screen_swaps_instead_of_losing_it(self):
        first = self._product("A-1")
        second = self._product("B-1")
        self._set_cart((first, 1))
        self.client.post(reverse("pos_screen"), {"action": "hold_cart", "hold_label": "Table 4"})
        self._set_cart((second, 2))
        held_id = self.client.session["held_carts"][0]["id"]

        self.client.post(reverse("pos_screen"), {"action": "resume_cart", "held_id": held_id})

        session = self.client.session
        self.assertEqual(list(session["cart"]), [str(first.id)])
        self.assertEqual(len(session["held_carts"]), 1)
        self.assertEqual(list(session["held_carts"][0]["cart"]), [str(second.id)])

    def test_discard_and_stale_ids(self):
        product = self._product()
        self._set_cart((product, 1))
        self.client.post(reverse("pos_screen"), {"action": "hold_cart", "hold_label": "Walk-in"})
        held_id = self.client.session["held_carts"][0]["id"]

        self.client.post(reverse("pos_screen"), {"action": "discard_held", "held_id": held_id})
        stale = self.client.post(reverse("pos_screen"), {"action": "resume_cart", "held_id": held_id}, follow=True)

        self.assertEqual(self.client.session["held_carts"], [])
        self.assertTrue(any("no longer available" in text for text in self._messages(stale)))

    def test_empty_cart_cannot_be_held(self):
        response = self.client.post(reverse("pos_screen"), {"action": "hold_cart"}, follow=True)

        self.assertTrue(any("Add at least one item" in text for text in self._messages(response)))
        self.assertFalse(self.client.session.get("held_carts"))

    def test_held_sale_can_be_checked_out_after_resume(self):
        product = self._product()
        self._set_cart((product, 2))
        self.client.post(reverse("pos_screen"), {"action": "hold_cart"})
        held_id = self.client.session["held_carts"][0]["id"]
        self.client.post(reverse("pos_screen"), {"action": "resume_cart", "held_id": held_id})

        response = self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"},
        )

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.stock, 3)


class NonStockItemTests(PosTestCase):
    """Services and other untracked items sell without a shelf quantity."""

    def test_products_track_stock_by_default(self):
        product = self._product(stock=0)

        self.assertTrue(tracks_stock(product))
        self.assertEqual(stock_shortfall(product, Decimal("1")), "Out of stock.")

    def test_untracked_item_never_reports_a_shortfall(self):
        service = SimpleNamespace(track_stock=False, stock=Decimal("0"), stock_display="0 pcs")

        self.assertFalse(tracks_stock(service))
        self.assertEqual(stock_shortfall(service, Decimal("3")), "")

    def test_untracked_item_can_be_added_to_the_cart_at_zero_stock(self):
        service = self._product("SRV-1", stock=0, track_stock=False)

        response = self.client.post(reverse("pos_screen"), {"action": "add", "product_id": service.id})
        page = self.client.get(reverse("pos_screen"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["cart"][str(service.id)]["quantity"], "1")
        self.assertEqual(page.context["cart_rows"][0]["stock_error"], "")


class PurchaseFlowTests(AuthenticatedAdminTestCase):
    def _line_payload(self, lines, *, initial=0):
        payload = {
            "items-TOTAL_FORMS": str(len(lines)),
            "items-INITIAL_FORMS": str(initial),
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
        }
        for index, line in enumerate(lines):
            for key, value in line.items():
                payload[f"items-{index}-{key}"] = str(value)
        return payload

    def _purchase(self, supplier, product, quantity=10):
        purchase = Purchase.objects.create(supplier=supplier, payment_method="cash", paid_amount=Decimal("0"))
        item = PurchaseItem.objects.create(
            purchase=purchase, product=product, quantity=quantity, cost_price=Decimal("10.00"), sale_price=Decimal("15.00")
        )
        purchase.refresh_from_db()
        return purchase, item

    def test_same_product_on_two_lines_is_accepted_and_costed_in_order(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("50.00"), stock=1)
        payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00"}
        payload.update(
            self._line_payload(
                [
                    {"product": product.id, "quantity": 3, "cost_price": "20.00", "sale_price": "50.00"},
                    {"product": product.id, "quantity": 4, "cost_price": "30.00", "sale_price": "50.00"},
                ]
            )
        )

        response = self.client.post(reverse("purchase_add"), payload)

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        purchase = Purchase.objects.get()
        self.assertEqual(purchase.items.count(), 2)
        self.assertEqual(purchase.total_amount, Decimal("180.00"))
        self.assertEqual(product.stock, 8)
        # (1 x 10 + 3 x 20 + 4 x 30) / 8
        self.assertEqual(product.cost_price, Decimal("23.75"))

    def test_posting_failure_on_add_rolls_back_into_a_form_error(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("50.00"), stock=1)
        payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00"}
        payload.update(self._line_payload([{"product": product.id, "quantity": 3, "cost_price": "20.00", "sale_price": "50.00"}]))

        with mock.patch("pos.views.purchases.post_purchase_from_pos", side_effect=ValueError("Cash account is missing")):
            response = self.client.post(reverse("purchase_add"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cash account is missing")
        product.refresh_from_db()
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertEqual(product.stock, 1)
        self.assertEqual(product.cost_price, Decimal("10.00"))

    def test_edit_below_already_sold_stock_is_a_form_error(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=0)
        purchase, item = self._purchase(supplier, product, quantity=10)
        Product.objects.filter(pk=product.pk).update(stock=2)  # eight were sold since
        payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00", "reason": "Supplier short-shipped"}
        payload.update(
            self._line_payload(
                [{"id": item.id, "product": product.id, "quantity": 1, "cost_price": "10.00", "sale_price": "15.00"}],
                initial=1,
            )
        )

        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been sold or adjusted out")
        item.refresh_from_db()
        purchase.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(item.quantity, 10)
        self.assertEqual(purchase.total_amount, Decimal("100.00"))
        self.assertEqual(purchase.edit_reason, "")
        self.assertEqual(product.stock, 2)
        self.assertEqual(purchase.edit_logs.count(), 0)

    def test_edit_below_returned_quantity_is_a_form_error(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=0)
        purchase, item = self._purchase(supplier, product, quantity=10)
        create_purchase_return(
            purchase=purchase,
            quantities={item.id: "4"},
            settlement_method="payable",
            bank_account=None,
            reason="Damaged",
            notes="",
            actor=self.admin_user,
        )
        payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00", "reason": "Fix count"}
        payload.update(
            self._line_payload(
                [{"id": item.id, "product": product.id, "quantity": 3, "cost_price": "10.00", "sale_price": "15.00"}],
                initial=1,
            )
        )

        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already returned to the supplier")
        item.refresh_from_db()
        self.assertEqual(item.quantity, 10)

    def test_removing_a_line_with_supplier_returns_is_a_form_error(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=0)
        other = Product.objects.create(name="Salt", sku="SALT-1", cost_price=Decimal("5.00"), price=Decimal("8.00"), stock=0)
        purchase, item = self._purchase(supplier, product, quantity=10)
        create_purchase_return(
            purchase=purchase,
            quantities={item.id: "1"},
            settlement_method="payable",
            bank_account=None,
            reason="Damaged",
            notes="",
            actor=self.admin_user,
        )
        payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00", "reason": "Wrong item"}
        payload.update(
            self._line_payload(
                [
                    {"id": item.id, "product": product.id, "quantity": 10, "cost_price": "10.00", "sale_price": "15.00", "DELETE": "on"},
                    {"product": other.id, "quantity": 2, "cost_price": "5.00", "sale_price": "8.00"},
                ],
                initial=1,
            )
        )

        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "supplier returns recorded against it")
        other.refresh_from_db()
        self.assertTrue(PurchaseItem.objects.filter(pk=item.pk).exists())
        self.assertEqual(other.stock, 0)

    def test_edit_can_remove_one_line_and_keep_another(self):
        supplier = Supplier.objects.create(name="ACME")
        rice = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=0)
        salt = Product.objects.create(name="Salt", sku="SALT-1", cost_price=Decimal("5.00"), price=Decimal("8.00"), stock=0)
        purchase, rice_item = self._purchase(supplier, rice, quantity=10)
        salt_item = PurchaseItem.objects.create(
            purchase=purchase, product=salt, quantity=4, cost_price=Decimal("5.00"), sale_price=Decimal("8.00")
        )
        payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00", "reason": "Salt never arrived"}
        payload.update(
            self._line_payload(
                [
                    {"id": rice_item.id, "product": rice.id, "quantity": 10, "cost_price": "10.00", "sale_price": "15.00"},
                    {"id": salt_item.id, "product": salt.id, "quantity": 4, "cost_price": "5.00", "sale_price": "8.00", "DELETE": "on"},
                ],
                initial=2,
            )
        )

        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 302)
        purchase.refresh_from_db()
        salt.refresh_from_db()
        self.assertEqual(purchase.total_amount, Decimal("100.00"))
        self.assertEqual(salt.stock, 0)
        self.assertFalse(PurchaseItem.objects.filter(pk=salt_item.pk).exists())
        self.assertEqual(purchase.edit_logs.count(), 1)


class TransactionAdminTests(AuthenticatedAdminTestCase):
    MODELS = (
        "sale",
        "saleitem",
        "purchase",
        "purchaseitem",
        "salereturn",
        "salereturnitem",
        "purchasereturn",
        "purchasereturnitem",
        "stockadjustment",
    )

    def setUp(self):
        super().setUp()
        self.superuser = get_user_model().objects.create_superuser("root", "root@example.com", "RootPass123!")
        self.client.force_login(self.superuser)

    def test_transaction_documents_are_read_only_in_admin(self):
        product = Product.objects.create(name="Tea", sku="TEA-1", cost_price=Decimal("5.00"), price=Decimal("9.00"), stock=5)
        sale = Sale.objects.create(payment_method="cash")
        SaleItem.objects.create(sale=sale, product=product, quantity=1, price=Decimal("9.00"), cost_price=Decimal("5.00"))

        for model in self.MODELS:
            with self.subTest(model=model):
                self.assertEqual(self.client.get(reverse(f"admin:pos_{model}_changelist")).status_code, 200)
                self.assertEqual(self.client.get(reverse(f"admin:pos_{model}_add")).status_code, 403)

        self.assertEqual(self.client.get(reverse("admin:pos_sale_change", args=[sale.id])).status_code, 200)
        self.assertEqual(self.client.post(reverse("admin:pos_sale_change", args=[sale.id]), {"paid_amount": "1"}).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:pos_sale_delete", args=[sale.id]), {"post": "yes"}).status_code, 403)
        product.refresh_from_db()
        self.assertTrue(Sale.objects.filter(pk=sale.pk).exists())
        self.assertEqual(product.stock, 4)

    def test_product_stock_is_not_editable_in_admin(self):
        product = Product.objects.create(name="Tea", sku="TEA-1", cost_price=Decimal("5.00"), price=Decimal("9.00"), stock=5)

        response = self.client.get(reverse("admin:pos_product_change", args=[product.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="stock"')


def _csv_upload(text, name="products.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


class CatalogImportExportTests(AuthenticatedAdminTestCase):
    HEADER = "name,sku,barcode,category,brand,supplier,unit,cost_price,markup_percent,price,minimum_margin_percent,opening_stock,low_stock_threshold,location,expiry_date"

    def _import(self, text, **extra):
        data = {"catalog_file": _csv_upload(text), "mode": "import"}
        data.update(extra)
        return self.client.post(reverse("product_import"), data)

    def test_template_download_has_every_column(self):
        response = self.client.get(reverse("product_import_template"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows[0], COLUMNS)
        self.assertEqual(rows[0][:15], self.HEADER.split(","))

    def test_import_creates_products_lookups_and_opening_stock(self):
        text = "\n".join(
            [
                self.HEADER,
                "Flour,FLOUR-1,,Grocery,Sunrise Mills,City Traders,kg,80.00,25,,10,50.5,10,Rack 1,2027-03-31",
                "Soap,,8901234567890,Toiletries,,,pcs,45.00,,60.00,,24,,,",
            ]
        )

        response = self._import(text)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('product_list')}?sort=recent")
        flour = Product.objects.get(sku="FLOUR-1")
        soap = Product.objects.get(name="Soap")
        self.assertEqual(flour.unit, "kg")
        self.assertEqual(flour.price, Decimal("100.00"))
        self.assertEqual(flour.markup_percent, Decimal("25"))
        self.assertEqual(flour.stock, Decimal("50.500"))
        self.assertEqual(flour.category.name, "Grocery")
        self.assertEqual(flour.brand.name, "Sunrise Mills")
        self.assertEqual(flour.supplier.name, "City Traders")
        self.assertEqual(str(flour.expiry_date), "2027-03-31")
        self.assertTrue(soap.sku.startswith("SKU-"))
        self.assertEqual(soap.barcode, "8901234567890")
        self.assertEqual(soap.stock, Decimal("24"))
        self.assertEqual(soap.low_stock_threshold, Decimal("5"))

        adjustment = StockAdjustment.objects.get(product=flour)
        self.assertEqual(adjustment.adjustment_mode, StockAdjustment.OPENING_STOCK)
        self.assertTrue(adjustment.voucher.lines.filter(account__title="Opening Balance Equity", credit=Decimal("4040.00")).exists())
        self.assertTrue(AuditLog.objects.filter(action="catalog_imported").exists())

    def test_every_bad_row_is_reported_and_nothing_is_saved(self):
        Product.objects.create(name="Existing", sku="DUP-1", cost_price=Decimal("1.00"), price=Decimal("2.00"))
        text = "\n".join(
            [
                self.HEADER,
                "Good Tea,TEA-1,,Drinks,,,pcs,10,,20,,5,,,",
                ",NONAME-1,,,,,pcs,10,,20,,,,,",
                "Bad Unit,BU-1,,,,,bushel,10,,20,,,,,",
                "No Price,NP-1,,,,,pcs,10,,,,,,,",
                "Half Piece,HP-1,,,,,pcs,10,,20,,2.5,,,",
                "Bad Number,BN-1,,,,,pcs,ten,,20,,,,,",
                "Bad Date,BD-1,,,,,pcs,10,,20,,,,,31/31/2027",
                "Clash,DUP-1,,,,,pcs,10,,20,,,,,",
                "Thin Margin,TM-1,,,,,pcs,100,,105,20,,,,",
                "Twin,TEA-1,,,,,pcs,10,,20,,,,,",
            ]
        )

        response = self._import(text)

        self.assertEqual(response.status_code, 200)
        errors = {error["row"]: " ".join(error["messages"]) for error in response.context["plan"].errors}
        self.assertEqual(sorted(errors), [3, 4, 5, 6, 7, 8, 9, 10, 11])
        self.assertIn("name: is required", errors[3])
        self.assertIn("unit: 'bushel' is not supported", errors[4])
        self.assertIn("price: enter a sale price", errors[5])
        self.assertIn("whole numbers", errors[6])
        self.assertIn("'ten' is not a number", errors[7])
        self.assertIn("is not a date", errors[8])
        self.assertIn("already belongs to 'Existing'", errors[9])
        self.assertIn("at least 120.00", errors[10])
        self.assertIn("also used on row 2", errors[11])
        self.assertContains(response, "Nothing was imported")
        self.assertEqual(Product.objects.count(), 1)
        self.assertFalse(Category.objects.filter(name="Drinks").exists())
        self.assertEqual(StockAdjustment.objects.count(), 0)

    def test_check_mode_validates_without_writing(self):
        text = "\n".join([self.HEADER, "Tea,TEA-1,,Drinks,,,pcs,10,,20,,5,,,"])

        response = self._import(text, mode="check")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The file is ready to import")
        self.assertContains(response, "New categories: Drinks")
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(Category.objects.count(), 0)

    def test_update_existing_changes_details_but_never_stock(self):
        category = Category.objects.create(name="grocery")
        product = Product.objects.create(
            name="Old Name", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=Decimal("7"), category=category
        )
        text = "\n".join([self.HEADER, "Basmati Rice,rice-1,,GROCERY,,,kg,12.00,,20.00,,99,,Rack 9,"])

        response = self.client.post(
            reverse("product_import"),
            {"catalog_file": _csv_upload(text), "mode": "import", "update_existing": "on"},
            follow=True,
        )

        product.refresh_from_db()
        self.assertEqual(product.name, "Basmati Rice")
        self.assertEqual(product.unit, "kg")
        self.assertEqual(product.price, Decimal("20.00"))
        self.assertEqual(product.location, "Rack 9")
        self.assertEqual(product.stock, Decimal("7"))
        self.assertEqual(product.category, category)
        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(StockAdjustment.objects.count(), 0)
        self.assertTrue(any("opening_stock was ignored" in str(message) for message in response.context["messages"]))

    def test_update_does_not_overwrite_stock_sold_while_the_file_was_read(self):
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=Decimal("7"))
        plan = parse_catalog(f"{self.HEADER}\nRice,RICE-1,,,,,pcs,10,,18,,,,,".encode(), update_existing=True)
        Product.objects.filter(pk=product.pk).update(stock=Decimal("3"))

        from pos.services.catalog_io import apply_catalog

        apply_catalog(plan, actor=self.admin_user)

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("3"))
        self.assertEqual(product.price, Decimal("18.00"))

    def test_semicolon_files_with_decimal_commas_are_understood(self):
        text = "name;sku;unit;cost_price;price;opening_stock\nMehl;MEHL-1;kg;1,20;1,99;12,5\n"

        response = self._import(text)

        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(sku="MEHL-1")
        self.assertEqual(product.cost_price, Decimal("1.20"))
        self.assertEqual(product.price, Decimal("1.99"))
        self.assertEqual(product.stock, Decimal("12.5"))

    def test_template_example_rows_are_skipped(self):
        template = self.client.get(reverse("product_import_template")).content.decode("utf-8-sig")

        response = self._import(template + "Real Tea,TEA-1,,,,,pcs,10,,20,,,,,\r\n")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Product.objects.values_list("sku", flat=True)), ["TEA-1"])

    def test_file_without_a_name_column_is_rejected(self):
        response = self._import("product,cost\nTea,10\n")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "include &#x27;name&#x27;")
        self.assertEqual(Product.objects.count(), 0)

    def test_export_round_trips_and_respects_list_filters(self):
        brand = Brand.objects.create(name="Sunrise")
        Product.objects.create(
            name="=HYPERLINK(1)", sku="FLOUR-1", unit="kg", cost_price=Decimal("80.00"), markup_percent=Decimal("25.00"),
            price=Decimal("100.00"), stock=Decimal("2.5"), brand=brand,
        )
        Product.objects.create(name="Soap", sku="SOAP-1", cost_price=Decimal("45.00"), price=Decimal("60.00"), stock=3)

        response = self.client.get(reverse("product_export"), {"brand": brand.id})

        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "FLOUR-1")
        self.assertEqual(rows[0]["name"], "'=HYPERLINK(1)")
        self.assertEqual(rows[0]["opening_stock"], "2.5")
        self.assertEqual(rows[0]["brand"], "Sunrise")

        # A full export is a valid update file for the same shop...
        plan = parse_catalog(export_catalog_csv_text().encode("utf-8"), update_existing=True)
        self.assertTrue(plan.is_valid, plan.errors)
        self.assertEqual(plan.update_count, 2)

        # ...and seeds a fresh install, stock included.
        Product.objects.all().delete()
        reimport = self.client.post(
            reverse("product_import"), {"catalog_file": SimpleUploadedFile("p.csv", response.content), "mode": "import"}
        )
        self.assertEqual(reimport.status_code, 302)
        restored = Product.objects.get(sku="FLOUR-1")
        self.assertEqual(restored.name, "=HYPERLINK(1)")
        self.assertEqual(restored.stock, Decimal("2.5"))
        self.assertEqual(restored.price, Decimal("100.00"))

    def test_cashier_cannot_import_or_export(self):
        cashier = get_user_model().objects.create_user(username="cashier", password="CashierPass123!")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)
        text = "\n".join([self.HEADER, "Tea,TEA-1,,,,,pcs,10,,20,,,,,"])

        import_response = self._import(text)
        export_response = self.client.get(reverse("product_export"))

        self.assertEqual(import_response.status_code, 302)
        self.assertNotEqual(import_response.url, f"{reverse('product_list')}?sort=recent")
        self.assertEqual(export_response.status_code, 302)
        self.assertEqual(Product.objects.count(), 0)

    def test_product_list_links_to_import_and_filtered_export(self):
        response = self.client.get(reverse("product_list"), {"status": "active"})

        self.assertContains(response, reverse("product_import"))
        self.assertContains(response, f"{reverse('product_export')}?status=active")

    def test_import_page_opens_with_template_link_and_column_guide(self):
        response = self.client.get(reverse("product_import"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("product_import_template"))
        self.assertContains(response, "opening_stock")


class ProductAddFlowTests(AuthenticatedAdminTestCase):
    PAYLOAD = {
        "name": "Sugar",
        "sku": "SUGAR-1",
        "unit": "kg",
        "cost_price": "90.00",
        "price": "110.00",
        "minimum_margin_percent": "0",
        "low_stock_threshold": "5",
        "opening_stock": "12.5",
        "is_active": "on",
    }

    def test_save_and_add_another_returns_to_a_blank_form(self):
        response = self.client.post(reverse("product_add"), {**self.PAYLOAD, "save_and_new": "1"})

        self.assertRedirects(response, reverse("product_add"))
        self.assertEqual(Product.objects.get(sku="SUGAR-1").stock, Decimal("12.5"))

    def test_opening_stock_failure_keeps_the_form_and_saves_nothing(self):
        with mock.patch("pos.views.products.create_stock_adjustment", side_effect=ValueError("Inventory account has the wrong type.")):
            response = self.client.post(reverse("product_add"), self.PAYLOAD)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inventory account has the wrong type.")
        self.assertFalse(Product.objects.filter(sku="SUGAR-1").exists())

    def _bread(self, **overrides):
        values = {"name": "Bread Small", "sku": "BREAD", "unit": "pcs", "cost_price": Decimal("60"), "price": Decimal("80"), "location": "Rack 2"}
        values.update(overrides)
        return Product.objects.create(**values)

    def test_sizes_of_one_item_are_saved_with_their_own_prices(self):
        for size, sku, price in (("Small", "BREAD-S", "80.00"), ("Medium", "BREAD-M", "120.00"), ("Large", "BREAD-L", "160.00")):
            response = self.client.post(
                reverse("product_add"),
                {**self.PAYLOAD, "name": f"Bread {size}", "sku": sku, "unit": "pcs", "cost_price": "50.00", "price": price, "opening_stock": ""},
            )
            self.assertEqual(response.status_code, 302)

        prices = dict(Product.objects.filter(name__startswith="Bread").values_list("name", "price"))
        self.assertEqual(
            prices,
            {"Bread Small": Decimal("80.00"), "Bread Medium": Decimal("120.00"), "Bread Large": Decimal("160.00")},
        )

    def test_another_size_starts_from_the_first_with_a_free_sku_and_no_price(self):
        source = self._bread()
        self._bread(name="Bread Medium", sku="BREAD-2")

        response = self.client.get(reverse("product_add"), {"copy": source.id})

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["name"].value(), "Bread Small")
        self.assertEqual(form["sku"].value(), "BREAD-3")
        self.assertEqual(form["location"].value(), "Rack 2")
        self.assertIn(form["price"].value(), (None, ""))
        self.assertIn(form["cost_price"].value(), (None, "", 0, Decimal("0")))
        self.assertContains(response, "Another size of")

    def test_save_and_add_another_size_continues_from_the_size_just_saved(self):
        source = self._bread()

        response = self.client.post(
            f"{reverse('product_add')}?copy={source.id}",
            {
                **self.PAYLOAD,
                "name": "Bread",
                "variant_label": "Large",
                "source_variant_label": "Small",
                "sku": "BREAD-2",
                "unit": "pcs",
                "price": "160.00",
                "opening_stock": "",
                "save_and_new": "1",
            },
        )

        saved = Product.objects.get(sku="BREAD-2")
        self.assertRedirects(response, f"{reverse('product_add')}?copy={saved.id}")
        self.assertEqual(saved.price, Decimal("160.00"))
        self.assertEqual(saved.display_name, "Bread - Large")
        source.refresh_from_db()
        self.assertEqual(source.price, Decimal("80.00"))
        # Adding a second size turns the first product into a size of the same family.
        self.assertEqual(source.variant_group_id, saved.variant_group_id)
        self.assertEqual(source.display_name, "Bread - Small")

    def test_unknown_copy_id_opens_a_blank_form(self):
        response = self.client.get(reverse("product_add"), {"copy": "999999"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Another size of")

    def test_product_page_and_list_offer_another_size(self):
        source = self._bread()
        link = f"{reverse('product_add')}?copy={source.id}"

        self.assertContains(self.client.get(reverse("product_view", args=[source.id])), link)
        self.assertContains(self.client.get(reverse("product_list")), link)


class SizesAtTheTillTests(PosTestCase):
    def _sizes(self):
        return [
            self._product(sku=sku, name=f"Bread {size}", cost_price=Decimal("50.00"), price=Decimal(price))
            for size, sku, price in (("Small", "BREAD-S", "80.00"), ("Medium", "BREAD-M", "120.00"), ("Large", "BREAD-L", "160.00"))
        ]

    def test_typing_the_item_name_finds_every_size(self):
        self._sizes()

        rows = self.client.get(reverse("till_catalogue")).json()["products"]

        found = {row["name"] for row in rows if "bread" in row["find"]}
        self.assertEqual(found, {"Bread Small", "Bread Medium", "Bread Large"})

    def test_sizes_sell_on_one_bill_at_their_own_prices(self):
        small, medium, large = self._sizes()
        self._set_cart((small, 2), (medium, 1), (large, 1))

        self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"},
        )

        sale = Sale.objects.get()
        sold = {item.product_id: (item.quantity, item.price) for item in SaleItem.objects.filter(sale=sale)}
        self.assertEqual(sold[small.id], (Decimal("2"), Decimal("80.00")))
        self.assertEqual(sold[medium.id], (Decimal("1"), Decimal("120.00")))
        self.assertEqual(sold[large.id], (Decimal("1"), Decimal("160.00")))
        for product, left in ((small, 3), (medium, 4), (large, 4)):
            product.refresh_from_db()
            self.assertEqual(product.stock, left)
