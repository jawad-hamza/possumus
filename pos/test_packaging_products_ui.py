"""Cartons and boxes on the product screens: the form, the list, the product page, stock adjustments, the CSV."""
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from pos.models import (
    PACK_BOX,
    PACK_CARTON,
    Product,
    Purchase,
    PurchaseItem,
    StockAdjustment,
    Supplier,
    SystemSettings,
)
from pos.services import modules
from pos.services.catalog_io import apply_catalog, export_catalog_csv_text, parse_catalog
from pos.test_support import AuthenticatedAdminTestCase


def _biscuits(**overrides):
    """A carton of 12 boxes, a box of 12 pieces: 144 pieces to the carton. Rs 1,000 a carton cost, 1,250 sale."""
    values = {
        "name": "Biscuits",
        "sku": "BISC",
        "pieces_per_box": 12,
        "boxes_per_carton": 12,
        "cost_price": Decimal("6.944444"),
        "price": Decimal("8.680556"),
        "minimum_margin_percent": Decimal("0"),
        "low_stock_threshold": Decimal("288"),
        "stock": Decimal("0"),
    }
    values.update(overrides)
    return Product.objects.create(**values)


def _edit_payload(product, **overrides):
    """What the edit page posts when nothing is touched: the figures exactly as the form shows them."""
    payload = {
        "name": product.name,
        "sku": product.sku,
        "unit": product.unit,
        "boxes_per_carton": str(product.boxes_per_carton),
        "pieces_per_box": str(product.pieces_per_box),
        "cost_price": "1000.00",
        "price": "1250.00",
        "price_by": "manual",
        "minimum_margin_percent": "0",
        "low_stock_threshold": "2",
        "low_stock_unit": PACK_CARTON,
        "is_active": "on",
    }
    payload.update(overrides)
    return payload


class ProductFormInPacksTests(AuthenticatedAdminTestCase):
    ADD = {
        "name": "Biscuits",
        "sku": "BISC",
        "unit": "pcs",
        "boxes_per_carton": "12",
        "pieces_per_box": "12",
        "cost_price": "1000.00",
        "price": "1250.00",
        "price_by": "manual",
        "minimum_margin_percent": "0",
        "opening_stock": "10",
        "opening_stock_unit": PACK_CARTON,
        "low_stock_threshold": "2",
        "low_stock_unit": PACK_CARTON,
        "is_active": "on",
    }

    def test_carton_figures_are_kept_per_piece_and_shown_back_per_carton(self):
        response = self.client.post(reverse("product_add"), self.ADD)
        self.assertEqual(response.status_code, 302, getattr(response, "context", {}) and response.context["form"].errors)

        product = Product.objects.get(sku="BISC")
        self.assertEqual(product.cost_price, Decimal("6.944444"))
        self.assertEqual(product.price, Decimal("8.680556"))
        self.assertEqual(product.stock, Decimal("1440"))
        self.assertEqual(product.low_stock_threshold, Decimal("288"))

        form = self.client.get(reverse("product_edit", args=[product.id])).context["form"]
        self.assertEqual(form["cost_price"].value(), Decimal("1000.00"))
        self.assertEqual(form["price"].value(), Decimal("1250.00"))
        self.assertEqual(form.fields["cost_price"].label, "Cost per carton")
        self.assertEqual(form.fields["price"].label, "Sale price per carton")
        # The alert level reads in the largest unit dividing it exactly.
        self.assertEqual(form["low_stock_threshold"].value(), "2")
        self.assertEqual(form["low_stock_unit"].value(), PACK_CARTON)

    def test_a_fixed_markup_is_typed_per_carton(self):
        payload = {**self.ADD, "price_by": "amount", "markup_amount": "288.00", "price": ""}
        response = self.client.post(reverse("product_add"), payload)
        self.assertEqual(response.status_code, 302)

        product = Product.objects.get(sku="BISC")
        self.assertEqual(product.markup_amount, Decimal("2.000000"))
        self.assertEqual(product.top_pack_price, Decimal("1288.00"))

    def test_untouched_figures_do_not_drift(self):
        # 6.944449 a piece reads as 1,000.00 a carton; dividing that again would give 6.944444.
        product = _biscuits(cost_price=Decimal("6.944449"), price=Decimal("8.680561"))

        response = self.client.post(reverse("product_edit", args=[product.id]), _edit_payload(product, name="Biscuits Gold"))
        self.assertEqual(response.status_code, 302)

        product.refresh_from_db()
        self.assertEqual(product.name, "Biscuits Gold")
        self.assertEqual(product.cost_price, Decimal("6.944449"))
        self.assertEqual(product.price, Decimal("8.680561"))
        self.assertEqual(product.low_stock_threshold, Decimal("288"))

    def test_a_typed_figure_is_divided_by_the_carton(self):
        product = _biscuits()

        response = self.client.post(reverse("product_edit", args=[product.id]), _edit_payload(product, price="1440.00"))
        self.assertEqual(response.status_code, 302)

        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("10.000000"))
        self.assertEqual(product.cost_price, Decimal("6.944444"))

    def test_a_packing_only_edit_keeps_the_piece_figures(self):
        product = _biscuits()

        # The carton shrinks to 6 boxes; the page still holds the old carton figures, untouched.
        response = self.client.post(reverse("product_edit", args=[product.id]), _edit_payload(product, boxes_per_carton="6"))
        self.assertEqual(response.status_code, 302)

        product.refresh_from_db()
        self.assertEqual(product.boxes_per_carton, 6)
        self.assertEqual(product.cost_price, Decimal("6.944444"))
        self.assertEqual(product.price, Decimal("8.680556"))
        # Two old cartons of alert level are still 288 pieces: now four cartons of 72.
        self.assertEqual(product.low_stock_threshold, Decimal("288"))
        self.assertEqual(product.top_pack_price, Decimal("625.00"))

    def test_a_price_typed_over_after_a_packing_change_is_for_the_new_carton(self):
        product = _biscuits()

        payload = _edit_payload(product, boxes_per_carton="6", pack_money_typed="price")
        response = self.client.post(reverse("product_edit", args=[product.id]), payload)
        self.assertEqual(response.status_code, 302)

        product.refresh_from_db()
        # 1,250 for a carton of 72 pieces.
        self.assertEqual(product.price, Decimal("17.361111"))
        self.assertEqual(product.cost_price, Decimal("6.944444"))

    def test_opening_stock_in_boxes_and_alert_in_pieces(self):
        payload = {
            **self.ADD,
            "opening_stock": "5",
            "opening_stock_unit": PACK_BOX,
            "low_stock_threshold": "30",
            "low_stock_unit": "piece",
        }
        response = self.client.post(reverse("product_add"), payload)
        self.assertEqual(response.status_code, 302)

        product = Product.objects.get(sku="BISC")
        self.assertEqual(product.stock, Decimal("60"))
        self.assertEqual(product.low_stock_threshold, Decimal("30"))
        form = self.client.get(reverse("product_edit", args=[product.id])).context["form"]
        self.assertEqual(form["low_stock_threshold"].value(), "30")
        self.assertEqual(form["low_stock_unit"].value(), "piece")

    def test_cartons_are_counted_whole(self):
        response = self.client.post(reverse("product_add"), {**self.ADD, "opening_stock": "1.5"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("opening_stock", response.context["form"].errors)
        self.assertFalse(Product.objects.filter(sku="BISC").exists())

    def test_a_unit_the_packing_does_not_give_is_refused(self):
        payload = {**self.ADD, "boxes_per_carton": "1"}
        response = self.client.post(reverse("product_add"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertIn("opening_stock", response.context["form"].errors)

    def test_a_new_product_starts_in_the_shops_stock_unit(self):
        form = self.client.get(reverse("product_add")).context["form"]

        self.assertIn("boxes_per_carton", form.fields)
        # No packing typed yet: the piece is the only level there is.
        self.assertEqual(form["opening_stock_unit"].value(), "piece")
        settings_row = SystemSettings.load()
        self.assertEqual(settings_row.stock_default_unit, PACK_CARTON)
        page = self.client.get(reverse("product_add")).content.decode()
        self.assertIn('data-stock-unit="carton"', page)
        self.assertIn('data-pack-line', page)


class ProductFormWithoutPacksTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        modules.set_module_switch("packaging", False)

    def test_the_form_is_plain_again(self):
        form = self.client.get(reverse("product_add")).context["form"]
        for name in ("boxes_per_carton", "pieces_per_box", "opening_stock_unit", "low_stock_unit"):
            self.assertNotIn(name, form.fields)
        self.assertEqual(form.fields["cost_price"].label, "Current cost")

        response = self.client.post(
            reverse("product_add"),
            {
                "name": "Soap",
                "sku": "SOAP",
                "unit": "pcs",
                "cost_price": "90.00",
                "price": "110.00",
                "minimum_margin_percent": "0",
                "opening_stock": "12",
                "low_stock_threshold": "5",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        soap = Product.objects.get(sku="SOAP")
        self.assertEqual(soap.cost_price, Decimal("90.00"))
        self.assertEqual(soap.price, Decimal("110.00"))
        self.assertEqual(soap.stock, Decimal("12"))
        self.assertEqual(soap.low_stock_threshold, Decimal("5"))
        self.assertNotIn("data-pack-line", self.client.get(reverse("product_edit", args=[soap.id])).content.decode())

    def test_a_packed_product_edits_per_piece_while_the_module_is_off(self):
        product = _biscuits()

        form = self.client.get(reverse("product_edit", args=[product.id])).context["form"]
        self.assertEqual(form["cost_price"].value(), Decimal("6.94"))
        self.assertEqual(form["low_stock_threshold"].value(), "288")

        payload = {
            "name": "Biscuits",
            "sku": "BISC",
            "unit": "pcs",
            "cost_price": "6.94",
            "price": "8.68",
            "minimum_margin_percent": "0",
            "low_stock_threshold": "288",
            "is_active": "on",
        }
        response = self.client.post(reverse("product_edit", args=[product.id]), payload)
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        # Shown as 6.94 and posted back untouched: still what was stored, and the packing is kept.
        self.assertEqual(product.cost_price, Decimal("6.944444"))
        self.assertEqual(product.price, Decimal("8.680556"))
        self.assertEqual(product.boxes_per_carton, 12)


def _buy(product, quantity, cost, *, unit=PACK_CARTON, supplier=None, bill=""):
    purchase = Purchase.objects.create(payment_method=Purchase.CASH, supplier=supplier, supplier_invoice_number=bill)
    item = PurchaseItem.objects.create(
        purchase=purchase,
        product=product,
        quantity=Decimal(str(quantity)),
        unit_level=unit,
        cost_price=Decimal(str(cost)),
        sale_price=Decimal("1250.00"),
    )
    product.refresh_from_db()
    return item


class ProductListInPacksTests(AuthenticatedAdminTestCase):
    def test_stock_reads_in_packs_from_the_chosen_unit(self):
        _biscuits(stock=Decimal("1503"))

        page = self.client.get(reverse("product_list")).content.decode()
        self.assertIn("10 Ctn 5 Box 3 pcs", page)
        self.assertIn("per Ctn", page)
        self.assertIn("1,250.00", page)

        by_box = self.client.get(reverse("product_list"), {"stock_unit": "box"})
        self.assertEqual(by_box.context["stock_unit"], PACK_BOX)
        self.assertIn("125 Box 3 pcs", by_box.content.decode())
        self.assertIn("1503 pcs", self.client.get(reverse("product_list"), {"stock_unit": "piece"}).content.decode())

    def test_low_and_out_badges_still_show(self):
        _biscuits(sku="LOW", stock=Decimal("200"))
        _biscuits(sku="OUT", name="Wafers", stock=Decimal("0"))

        page = self.client.get(reverse("product_list")).content.decode()
        self.assertIn(">Low<", page)
        self.assertIn(">Out<", page)
        self.assertIn("Low at 2 Ctn", page)

    def test_recent_purchases_show_in_the_unit_bought(self):
        product = _biscuits()
        supplier = Supplier.objects.create(name="Ali Traders")
        line = _buy(product, 2, "1000.00", supplier=supplier, bill="INV-77")

        response = self.client.get(reverse("product_list"))
        page = response.content.decode()
        self.assertEqual(list(response.context["recent_purchase_lines"]), [line])
        self.assertEqual(response.context["products"].object_list[0].last_purchase, line)
        self.assertIn("Recent purchases", page)
        self.assertIn("Ali Traders", page)
        self.assertIn("2 Ctn", page)
        self.assertIn('id="col-lastbuy"', page)

    def test_the_last_purchase_costs_no_query_per_row(self):
        def queries_for_list():
            with CaptureQueriesContext(connection) as captured:
                self.client.get(reverse("product_list"))
            return len(captured)

        first = _biscuits(sku="A")
        _buy(first, 1, "1000.00")
        few = queries_for_list()
        for index in range(4):
            other = _biscuits(sku=f"B{index}", name=f"Biscuits {index}")
            _buy(other, 1, "1000.00")
        self.assertEqual(queries_for_list(), few)


class ProductPageInPacksTests(AuthenticatedAdminTestCase):
    def test_packing_prices_stock_and_recent_lines(self):
        product = _biscuits(stock=Decimal("0"))
        supplier = Supplier.objects.create(name="Ali Traders")
        _buy(product, 3, "1000.00", supplier=supplier, bill="INV-77")

        response = self.client.get(reverse("product_view", args=[product.id]))
        page = response.content.decode()
        self.assertIn("1 Ctn = 12 Box = 144 pcs", page)
        self.assertIn("Price by pack", page)
        self.assertIn("104.17", page)  # a box at 1,250 a carton
        self.assertIn("3 Ctn", page)  # stock and the purchase line
        self.assertIn("Low at 2 Ctn", page)
        self.assertIn("INV-77", page)
        self.assertIn("Ali Traders", page)
        self.assertIn("per Ctn", page)
        self.assertNotIn("6.944444", page)


class StockAdjustmentInPacksTests(AuthenticatedAdminTestCase):
    def _post(self, product, **fields):
        payload = {
            "product": product.id,
            "adjustment_mode": StockAdjustment.ADJUSTMENT,
            "reason_preset": "opening_balance",
            "reason": "Opening Balance Correction",
        }
        payload.update(fields)
        return self.client.post(reverse("stock_adjustments"), payload, follow=True)

    def test_an_adjustment_in_cartons_moves_pieces(self):
        product = _biscuits(stock=Decimal("144"))

        response = self._post(product, quantity_delta="2", quantity_delta_unit=PACK_CARTON)

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("432"))
        adjustment = StockAdjustment.objects.get(product=product)
        self.assertEqual(adjustment.quantity_delta, Decimal("288"))
        self.assertContains(response, "3 Ctn on hand")

    def test_a_stock_take_in_boxes(self):
        product = _biscuits(stock=Decimal("144"))

        self._post(
            product,
            adjustment_mode=StockAdjustment.STOCK_TAKE,
            quantity_after="15",
            quantity_after_unit=PACK_BOX,
            reason_preset="stock_take_difference",
            reason="Stock Take Difference",
        )

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("180"))
        page = self.client.get(reverse("stock_adjustments")).content.decode()
        self.assertIn("1 Ctn 3 Box", page)

    def test_part_of_a_carton_is_refused(self):
        product = _biscuits(stock=Decimal("144"))

        response = self._post(product, quantity_delta="1.5", quantity_delta_unit=PACK_CARTON)

        self.assertIn("quantity_delta", response.context["form"].errors)
        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("144"))

    def test_a_plain_product_ignores_the_unit(self):
        soap = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("10"), price=Decimal("20"), stock=Decimal("5"))

        self._post(soap, quantity_delta="2", quantity_delta_unit=PACK_CARTON)

        soap.refresh_from_db()
        self.assertEqual(soap.stock, Decimal("7"))


class CatalogCsvInPacksTests(AuthenticatedAdminTestCase):
    def test_export_then_import_changes_nothing(self):
        product = _biscuits(cost_price=Decimal("6.944449"), stock=Decimal("1503"))
        text = export_catalog_csv_text()
        self.assertIn("pieces_per_box", text.splitlines()[0])
        self.assertIn("1000.00", text)
        self.assertIn("1250.00", text)
        self.assertIn("1503", text)

        plan = parse_catalog(text.encode("utf-8"), update_existing=True)
        self.assertTrue(plan.is_valid, plan.errors)
        apply_catalog(plan, actor=self.admin_user)

        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("6.944449"))
        self.assertEqual(product.price, Decimal("8.680556"))
        self.assertEqual((product.pieces_per_box, product.boxes_per_carton), (12, 12))
        self.assertEqual(product.stock, Decimal("1503"))
        self.assertEqual(product.low_stock_threshold, Decimal("288"))

    def test_a_fresh_install_reads_the_carton_prices(self):
        _biscuits(stock=Decimal("1503"))
        text = export_catalog_csv_text()
        Product.objects.all().delete()

        plan = parse_catalog(text.encode("utf-8"))
        self.assertTrue(plan.is_valid, plan.errors)
        apply_catalog(plan, actor=self.admin_user)

        restored = Product.objects.get(sku="BISC")
        self.assertEqual(restored.top_pack_cost, Decimal("1000.00"))
        self.assertEqual(restored.top_pack_price, Decimal("1250.00"))
        self.assertEqual(restored.cost_price, Decimal("6.944444"))
        self.assertEqual(restored.stock, Decimal("1503"))
