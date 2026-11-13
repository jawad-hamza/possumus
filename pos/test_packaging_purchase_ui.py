"""Cartons, boxes and pieces on the purchase screens and the return screens."""
from decimal import Decimal

from django.urls import reverse

from pos.forms import PurchaseItemForm, QuickProductForm
from pos.models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_PIECE,
    Product,
    Purchase,
    PurchaseItem,
    Sale,
    SaleItem,
    Supplier,
    SystemSettings,
)
from pos.services import modules
from pos.test_support import AuthenticatedAdminTestCase


def _lines(lines, *, initial=0):
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


def _purchase_payload(lines, **extra):
    # Left unpaid, so the balance goes on a supplier's account.
    supplier, _ = Supplier.objects.get_or_create(name="Wholesaler")
    payload = {"supplier": supplier.id, "payment_method": "cash", "paid_amount": "0.00"}
    payload.update(_lines(lines, initial=extra.pop("initial", 0)))
    payload.update(extra)
    return payload


def _biscuits(**overrides):
    values = {
        "name": "Biscuits",
        "sku": "BISC",
        "pieces_per_box": 12,
        "boxes_per_carton": 12,
        "cost_price": Decimal("0"),
        "price": Decimal("0"),
        "stock": Decimal("0"),
    }
    values.update(overrides)
    return Product.objects.create(**values)


class PurchaseLineFormTests(AuthenticatedAdminTestCase):
    def test_a_line_offers_a_unit_and_the_packing_of_its_product(self):
        product = _biscuits()
        form = PurchaseItemForm(prefix="items-0")

        html = str(form["product"])
        self.assertIn('data-pieces-per-box="12"', html)
        self.assertIn('data-boxes-per-carton="12"', html)
        self.assertIn("unit_level", form.fields)
        self.assertIn("pieces_per_box", form.fields)
        # A blank line starts in the shop's stock unit.
        self.assertEqual(form["unit_level"].value(), PACK_CARTON)
        self.assertTrue(product.has_packaging)

    def test_the_starting_unit_follows_the_setting(self):
        settings = SystemSettings.load()
        settings.stock_default_unit = PACK_BOX
        settings.save()

        form = PurchaseItemForm(prefix="items-0")

        self.assertEqual(form["unit_level"].value(), PACK_BOX)

    def test_the_fixed_markup_and_price_are_given_per_piece(self):
        _biscuits(sku="M", markup_amount=Decimal("0.5"), price=Decimal("8.680556"))

        html = str(PurchaseItemForm(prefix="items-0")["product"])

        self.assertIn('data-markup-amount="0.500000"', html)
        self.assertIn('data-price="8.680556"', html)

    def test_a_saved_line_offers_its_own_unit(self):
        product = _biscuits()
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)
        item = PurchaseItem.objects.create(
            purchase=purchase, product=product, quantity=2, unit_level=PACK_CARTON,
            cost_price=Decimal("1000"), sale_price=Decimal("1250"),
        )

        form = PurchaseItemForm(instance=item, prefix="items-0")

        self.assertEqual(form["unit_level"].value(), PACK_CARTON)
        self.assertEqual(form.saved_line["factor"], "144")
        self.assertEqual(form["pieces_per_box"].value(), 12)
        self.assertEqual(form.fields["quantity"].widget.attrs["step"], "1")


class PurchaseInCartonsTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        # Bought loose until now: the packing is typed on the purchase line.
        self.product = Product.objects.create(
            name="Juice", sku="JUICE", cost_price=Decimal("5"), price=Decimal("8"), stock=Decimal("0")
        )

    def _buy_in_cartons(self):
        payload = _purchase_payload(
            [
                {
                    "product": self.product.id,
                    "quantity": "10",
                    "unit_level": PACK_CARTON,
                    "boxes_per_carton": "12",
                    "pieces_per_box": "12",
                    "cost_price": "1000.00",
                    "sale_price": "1250.00",
                }
            ]
        )
        return self.client.post(reverse("purchase_add"), payload)

    def test_cartons_are_bought_with_the_packing_typed_on_the_line(self):
        response = self._buy_in_cartons()

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        item = PurchaseItem.objects.get()
        self.assertEqual((self.product.pieces_per_box, self.product.boxes_per_carton), (12, 12))
        self.assertEqual(item.unit_level, PACK_CARTON)
        self.assertEqual(item.unit_factor, Decimal("144"))
        self.assertEqual(self.product.stock, Decimal("1440"))
        self.assertEqual(self.product.price, Decimal("8.680556"))
        self.assertEqual(Purchase.objects.get().total_amount, Decimal("10000.00"))

    def test_the_purchase_shows_cartons_and_the_pieces_they_hold(self):
        self._buy_in_cartons()
        purchase = Purchase.objects.get()

        page = self.client.get(reverse("purchase_detail", args=[purchase.id]))

        self.assertContains(page, "10 Ctn")
        self.assertContains(page, "1,440 pcs")
        self.assertContains(page, "/ Ctn")

    def test_editing_the_packing_on_the_line_resizes_the_saved_line(self):
        self._buy_in_cartons()
        purchase = Purchase.objects.get()
        item = purchase.items.get()

        page = self.client.get(reverse("purchase_edit", args=[purchase.id]))
        self.assertContains(page, 'data-saved-level="carton"')
        self.assertContains(page, 'data-saved-factor="144"')

        payload = _purchase_payload(
            [
                {
                    "id": item.id,
                    "product": self.product.id,
                    "quantity": "10",
                    "unit_level": PACK_CARTON,
                    "boxes_per_carton": "10",
                    "pieces_per_box": "12",
                    "cost_price": "1000.00",
                    "sale_price": "1250.00",
                }
            ],
            initial=1,
            reason="Cartons hold ten boxes",
        )
        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.product.boxes_per_carton, 10)
        self.assertEqual(item.unit_factor, Decimal("120"))
        self.assertEqual(self.product.stock, Decimal("1200"))

    def test_editing_only_the_quantity_keeps_the_unit(self):
        self._buy_in_cartons()
        purchase = Purchase.objects.get()
        item = purchase.items.get()

        payload = _purchase_payload(
            [{"id": item.id, "product": self.product.id, "quantity": "8", "cost_price": "1000.00", "sale_price": "1250.00"}],
            initial=1,
            reason="Two cartons short",
        )
        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(item.unit_level, PACK_CARTON)
        self.assertEqual(self.product.stock, Decimal("1152"))

    def test_a_carton_must_be_bought_whole(self):
        payload = _purchase_payload(
            [
                {
                    "product": self.product.id,
                    "quantity": "1.5",
                    "unit_level": PACK_CARTON,
                    "boxes_per_carton": "12",
                    "pieces_per_box": "12",
                    "cost_price": "1000.00",
                    "sale_price": "1250.00",
                }
            ]
        )

        response = self.client.post(reverse("purchase_add"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Purchase.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.boxes_per_carton, 1)

    def test_a_line_without_a_unit_is_in_pieces(self):
        payload = _purchase_payload(
            [{"product": self.product.id, "quantity": "5", "cost_price": "6.00", "sale_price": "9.00"}]
        )

        self.client.post(reverse("purchase_add"), payload)

        item = PurchaseItem.objects.get()
        self.assertEqual(item.unit_level, PACK_PIECE)
        self.assertEqual(item.unit_factor, Decimal("1"))

    def test_quick_product_takes_packing_and_a_carton_price(self):
        response = self.client.post(
            reverse("quick_create_product"),
            {
                "quick_product-name": "Wafers",
                "quick_product-sku": "WAF-1",
                "quick_product-unit": "pcs",
                "quick_product-boxes_per_carton": "12",
                "quick_product-pieces_per_box": "12",
                "quick_product-price": "1250",
                "quick_product-minimum_margin_percent": "0",
                "quick_product-low_stock_threshold": "5",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200, response.content)
        product = Product.objects.get(sku="WAF-1")
        self.assertEqual((product.pieces_per_box, product.boxes_per_carton), (12, 12))
        self.assertEqual(product.price, Decimal("8.680556"))
        attrs = response.json()["product"]["option_attrs"]
        self.assertEqual(attrs["data-pieces-per-box"], "12")
        self.assertEqual(attrs["data-price"], "8.680556")


class PackagingOffTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        modules.set_module_switch("packaging", False)
        self.product = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("10"), price=Decimal("15"))

    def test_the_purchase_screen_has_no_units_or_packing(self):
        form = PurchaseItemForm(prefix="items-0")
        page = self.client.get(reverse("purchase_add"))

        self.assertNotIn("unit_level", form.fields)
        self.assertNotIn("pieces_per_box", form.fields)
        self.assertNotContains(page, "unit_level")
        self.assertNotContains(page, "data-line-packing")
        self.assertNotContains(page, "data-pieces-per-box")
        self.assertContains(page, "data-line-unit")

    def test_a_purchase_is_posted_as_before(self):
        payload = _purchase_payload(
            [{"product": self.product.id, "quantity": "4", "cost_price": "10.00", "sale_price": "15.00"}]
        )

        response = self.client.post(reverse("purchase_add"), payload)

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, Decimal("4"))
        self.assertEqual(PurchaseItem.objects.get().unit_level, PACK_PIECE)

    def test_the_quick_product_form_has_no_packing(self):
        self.assertNotIn("pieces_per_box", QuickProductForm(prefix="quick_product").fields)


class ReturnScreensInPacksTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.product = _biscuits()
        self.purchase = Purchase.objects.create(payment_method=Purchase.CASH)
        self.purchase_item = PurchaseItem.objects.create(
            purchase=self.purchase, product=self.product, quantity=10, unit_level=PACK_CARTON,
            cost_price=Decimal("1000.00"), sale_price=Decimal("1250.00"),
        )
        self.product.refresh_from_db()

    def test_the_purchase_return_screen_offers_smaller_units(self):
        page = self.client.get(reverse("purchase_return", args=[self.purchase.id]))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'name="unit_{self.purchase_item.id}"')
        self.assertContains(page, 'value="carton" data-max="10" data-step="1" selected')
        self.assertContains(page, 'value="piece" data-max="1440" data-step="1"')
        self.assertContains(page, "10 Ctn")

    def test_pieces_go_back_to_the_supplier_from_a_carton_line(self):
        response = self.client.post(
            reverse("purchase_return", args=[self.purchase.id]),
            {
                f"qty_{self.purchase_item.id}": "36",
                f"unit_{self.purchase_item.id}": PACK_PIECE,
                "settlement_method": "cash",
                "reason": "Damaged",
            },
        )

        self.assertEqual(response.status_code, 302, response.content[:2000])
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, Decimal("1404"))
        page = self.client.get(reverse("purchase_return", args=[self.purchase.id]))
        self.assertContains(page, "Biscuits &times; 36 pcs")
        detail = self.client.get(reverse("purchase_detail", args=[self.purchase.id]))
        self.assertContains(detail, "Returned to supplier")
        self.assertContains(detail, "36 pcs")

    def test_the_sale_return_screen_offers_smaller_units(self):
        sale = Sale.objects.create(payment_method=Sale.CASH, sold_by=self.admin_user)
        item = SaleItem.objects.create(sale=sale, product=self.product, quantity=Decimal("2"), unit_level=PACK_CARTON)

        page = self.client.get(reverse("sale_return", args=[sale.id]))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'name="unit_{item.id}"')
        self.assertContains(page, 'value="box" data-max="24" data-step="1"')

        response = self.client.post(
            reverse("sale_return", args=[sale.id]),
            {f"qty_{item.id}": "3", f"unit_{item.id}": PACK_BOX, "refund_method": "cash", "reason": "Crushed"},
        )

        self.assertEqual(response.status_code, 302)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, Decimal("1440") - Decimal("288") + Decimal("36"))

    def test_a_plain_line_has_no_unit_choice(self):
        plain = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("10"), price=Decimal("15"))
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)
        item = PurchaseItem.objects.create(purchase=purchase, product=plain, quantity=5, cost_price=Decimal("10"), sale_price=Decimal("15"))

        page = self.client.get(reverse("purchase_return", args=[purchase.id]))

        self.assertNotContains(page, f'name="unit_{item.id}"')
        self.assertContains(page, f'name="qty_{item.id}"')
