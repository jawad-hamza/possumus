"""Cartons, boxes and pieces: stock is counted in pieces, lines are kept in the unit they were typed in."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.urls import reverse

from pos.models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_PIECE,
    Product,
    Purchase,
    PurchaseItem,
    Sale,
    SaleItem,
    SystemSettings,
)
from pos.services import modules
from pos.services.cart_pricing import seed_cart_item
from pos.services.reporting import best_selling_products, build_sales_by_product, build_stock_valuation
from pos.services.returns import create_purchase_return, create_sale_return, return_row
from pos.test_support import AuthenticatedAdminTestCase


AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", "HTTP_ACCEPT": "application/json"}


def _biscuits(**overrides):
    """A carton of 12 boxes, a box of 12 pieces: 144 pieces to the carton."""
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


def _buy(product, quantity, cost, sale_price, unit=PACK_CARTON, purchase=None):
    purchase = purchase or Purchase.objects.create(payment_method=Purchase.CASH)
    item = PurchaseItem.objects.create(
        purchase=purchase,
        product=product,
        quantity=Decimal(str(quantity)),
        unit_level=unit,
        cost_price=Decimal(str(cost)),
        sale_price=Decimal(str(sale_price)),
    )
    product.refresh_from_db()
    return item


def _sell(product, quantity, unit=PACK_PIECE, **extra):
    sale = Sale.objects.create(payment_method=Sale.CASH)
    item = SaleItem.objects.create(sale=sale, product=product, quantity=Decimal(str(quantity)), unit_level=unit, **extra)
    product.refresh_from_db()
    return item


class PackLevelTests(AuthenticatedAdminTestCase):
    def test_a_product_has_the_levels_its_packing_gives_it(self):
        full = _biscuits()
        carton_of_pieces = _biscuits(sku="LOOSE", pieces_per_box=1, boxes_per_carton=24)
        boxed_only = _biscuits(sku="BOXED", pieces_per_box=10, boxes_per_carton=1)
        plain = _biscuits(sku="PLAIN", pieces_per_box=1, boxes_per_carton=1)

        self.assertEqual(full.pack_level_codes, [PACK_CARTON, PACK_BOX, PACK_PIECE])
        self.assertEqual(full.pack_factor(PACK_CARTON), Decimal("144"))
        self.assertEqual(full.pack_factor(PACK_BOX), Decimal("12"))
        self.assertEqual(carton_of_pieces.pack_level_codes, [PACK_CARTON, PACK_PIECE])
        self.assertEqual(carton_of_pieces.pack_factor(PACK_CARTON), Decimal("24"))
        self.assertEqual(boxed_only.pack_level_codes, [PACK_BOX, PACK_PIECE])
        self.assertEqual(plain.pack_level_codes, [PACK_PIECE])
        self.assertFalse(plain.has_packaging)
        self.assertEqual(full.packing_display, "1 Ctn = 12 Box = 144 pcs")
        self.assertEqual(plain.packing_display, "")

    def test_a_missing_level_falls_to_the_next_smaller_one(self):
        carton_of_pieces = _biscuits(sku="LOOSE", pieces_per_box=1, boxes_per_carton=24)
        plain = _biscuits(sku="PLAIN", pieces_per_box=1, boxes_per_carton=1)

        self.assertEqual(carton_of_pieces.resolve_pack_level(PACK_BOX), PACK_PIECE)
        self.assertEqual(carton_of_pieces.resolve_pack_level(PACK_CARTON), PACK_CARTON)
        self.assertEqual(plain.resolve_pack_level(PACK_CARTON), PACK_PIECE)

    def test_stock_reads_in_cartons_boxes_and_pieces(self):
        product = _biscuits(stock=Decimal("1769"))

        self.assertEqual(product.stock_display, "12 Ctn 3 Box 5 pcs")
        self.assertEqual(product.format_quantity(Decimal("1769"), PACK_BOX), "147 Box 5 pcs")
        self.assertEqual(product.format_quantity(Decimal("1769"), PACK_PIECE), "1769 pcs")
        self.assertEqual(product.format_quantity(Decimal("144")), "1 Ctn")
        self.assertEqual(product.format_quantity(Decimal("0")), "0 Ctn")
        self.assertEqual(product.format_quantity(Decimal("-13")), "-1 Box 1 pcs")

    def test_a_plain_product_reads_as_before(self):
        plain = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("10"), price=Decimal("25"), stock=5)

        self.assertEqual(plain.stock_display, "5 pcs")
        self.assertEqual(plain.pack_price(PACK_CARTON), Decimal("25.00"))

    def test_switching_the_module_off_makes_every_product_plain(self):
        product = _biscuits(stock=Decimal("288"))
        modules.set_module_switch("packaging", False)

        self.assertEqual(product.pack_level_codes, [PACK_PIECE])
        self.assertEqual(product.stock_display, "288 pcs")
        self.assertEqual(product.pack_factor(PACK_CARTON), Decimal("1"))

    def test_a_product_packed_in_boxes_is_counted_in_pieces_whatever_unit_was_picked(self):
        product = Product(name="X", sku="X", unit="box", pieces_per_box=6, cost_price=Decimal("1"), price=Decimal("2"))

        product.full_clean()

        self.assertEqual(product.unit, Product.UNIT_PIECE)
        self.assertEqual(product.pack_level_codes, [PACK_BOX, PACK_PIECE])


class PurchaseInPacksTests(AuthenticatedAdminTestCase):
    def test_a_carton_price_gives_the_box_and_piece_price(self):
        product = _biscuits()

        item = _buy(product, 10, "1000.00", "1250.00")

        self.assertEqual(item.unit_factor, Decimal("144"))
        self.assertEqual(item.base_quantity, Decimal("1440"))
        self.assertEqual(item.line_total, Decimal("10000.00"))
        self.assertEqual(item.purchase.total_amount, Decimal("10000.00"))
        self.assertEqual(product.stock, Decimal("1440"))
        # One piece keeps six places, so the carton price comes back exactly.
        self.assertEqual(product.cost_price, Decimal("6.944444"))
        self.assertEqual(product.pack_cost(PACK_CARTON), Decimal("1000.00"))
        self.assertEqual(product.pack_cost(PACK_BOX), Decimal("83.33"))
        self.assertEqual(product.pack_cost(PACK_PIECE), Decimal("6.94"))
        self.assertEqual(product.pack_price(PACK_CARTON), Decimal("1250.00"))
        self.assertEqual(product.pack_price(PACK_BOX), Decimal("104.17"))
        self.assertEqual(product.pack_price(PACK_PIECE), Decimal("8.68"))

    def test_stock_value_matches_what_was_paid(self):
        product = _biscuits()

        _buy(product, 100, "999.99", "1250.00")

        self.assertEqual((product.stock * product.cost_price).quantize(Decimal("0.01")), Decimal("99999.00"))

    def test_the_average_cost_blends_cartons_and_boxes(self):
        product = _biscuits()
        _buy(product, 1, "1440.00", "2000.00")  # 144 pcs at 10.00
        _buy(product, 12, "150.00", "200.00", unit=PACK_BOX)  # 144 pcs at 12.50

        self.assertEqual(product.stock, Decimal("288"))
        self.assertEqual(product.cost_price, Decimal("11.250000"))
        self.assertEqual(product.pack_cost(PACK_CARTON), Decimal("1620.00"))
        # The last line was priced by the box: 200 a box is 2,400 a carton.
        self.assertEqual(product.pack_price(PACK_CARTON), Decimal("2400.00"))

    def test_cartons_are_bought_whole(self):
        product = _biscuits()
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)

        with self.assertRaises(ValidationError):
            PurchaseItem.objects.create(
                purchase=purchase, product=product, quantity=Decimal("1.5"), unit_level=PACK_CARTON,
                cost_price=Decimal("1000"), sale_price=Decimal("1250"),
            )

    def test_a_saved_line_keeps_its_size_when_the_packing_changes(self):
        product = _biscuits()
        item = _buy(product, 2, "1000.00", "1250.00")
        Product.objects.filter(pk=product.pk).update(boxes_per_carton=10)

        item = PurchaseItem.objects.get(pk=item.pk)
        item.save()
        product.refresh_from_db()

        self.assertEqual(item.unit_factor, Decimal("144"))
        self.assertEqual(product.stock, Decimal("288"))

    def test_editing_the_quantity_moves_stock_in_pieces(self):
        product = _biscuits()
        item = _buy(product, 2, "1000.00", "1250.00")

        item = PurchaseItem.objects.get(pk=item.pk)
        item.quantity = Decimal("5")
        item.save()
        product.refresh_from_db()

        self.assertEqual(product.stock, Decimal("720"))
        self.assertEqual(product.pack_cost(PACK_CARTON), Decimal("1000.00"))

    def test_a_fixed_markup_is_added_for_every_piece_in_the_pack(self):
        product = _biscuits(markup_amount=Decimal("1"))
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)

        item = PurchaseItem(purchase=purchase, product=product, quantity=Decimal("1"), unit_level=PACK_CARTON, cost_price=Decimal("1000"))
        item.save()
        product.refresh_from_db()

        self.assertEqual(item.sale_price, Decimal("1144.00"))
        self.assertEqual(product.pack_price(PACK_CARTON), Decimal("1144.00"))

    def test_a_plain_product_keeps_two_place_average_cost(self):
        plain = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("0"), price=Decimal("0"), stock=0)
        _buy(plain, 3, "10.00", "15.00", unit=PACK_PIECE)
        _buy(plain, 3, "10.01", "15.00", unit=PACK_PIECE)

        self.assertEqual(plain.cost_price, Decimal("10.01"))


class SaleInPacksTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.product = _biscuits()
        _buy(self.product, 10, "1000.00", "1250.00")

    def test_selling_cartons_takes_pieces_off_the_shelf(self):
        item = _sell(self.product, 2, PACK_CARTON)

        self.assertEqual(item.unit_factor, Decimal("144"))
        self.assertEqual(item.price, Decimal("1250.00"))
        self.assertEqual(item.line_total, Decimal("2500.00"))
        self.assertEqual(item.line_cost, Decimal("2000.00"))
        self.assertEqual(item.sale.total_amount, Decimal("2500.00"))
        self.assertEqual(self.product.stock, Decimal("1152"))
        self.assertEqual(item.quantity_display, "2 Ctn")

    def test_selling_boxes_and_pieces(self):
        box_line = _sell(self.product, 5, PACK_BOX)
        piece_line = _sell(self.product, 7, PACK_PIECE)

        self.assertEqual(box_line.line_total, Decimal("520.85"))
        self.assertEqual(piece_line.price, Decimal("8.68"))
        self.assertEqual(piece_line.line_cost, Decimal("48.61"))
        self.assertEqual(self.product.stock, Decimal("1440") - Decimal("60") - Decimal("7"))

    def test_a_large_carton_sale_costs_exactly_what_was_paid(self):
        item = _sell(self.product, 10, PACK_CARTON)

        self.assertEqual(item.line_cost, Decimal("10000.00"))

    def test_cartons_are_sold_whole_and_within_stock(self):
        sale = Sale.objects.create(payment_method=Sale.CASH)

        with self.assertRaises(ValidationError):
            SaleItem.objects.create(sale=sale, product=self.product, quantity=Decimal("0.5"), unit_level=PACK_CARTON)
        with self.assertRaises(ValidationError):
            SaleItem.objects.create(sale=sale, product=self.product, quantity=Decimal("11"), unit_level=PACK_CARTON)

    def test_the_minimum_margin_is_checked_in_the_units_of_the_line(self):
        Product.objects.filter(pk=self.product.pk).update(minimum_margin_percent=Decimal("10"))
        self.product.refresh_from_db()
        sale = Sale.objects.create(payment_method=Sale.CASH)

        with self.assertRaises(ValidationError):
            SaleItem.objects.create(
                sale=sale, product=self.product, quantity=Decimal("1"), unit_level=PACK_CARTON, price=Decimal("1099.99")
            )
        item = SaleItem.objects.create(
            sale=sale, product=self.product, quantity=Decimal("1"), unit_level=PACK_CARTON, price=Decimal("1100.00")
        )

        self.assertEqual(item.line_total, Decimal("1100.00"))

    def test_deleting_a_line_puts_the_pieces_back(self):
        item = _sell(self.product, 3, PACK_BOX)

        item.delete()
        self.product.refresh_from_db()

        self.assertEqual(self.product.stock, Decimal("1440"))

    def test_best_sellers_count_in_pieces(self):
        _sell(self.product, 1, PACK_CARTON)
        _sell(self.product, 2, PACK_BOX)
        _sell(self.product, 3, PACK_PIECE)

        rows = best_selling_products()

        self.assertEqual(rows[0]["total_qty"], Decimal("171"))


class ReportsInPacksTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.product = _biscuits()
        _buy(self.product, 10, "1000.00", "1250.00")
        _sell(self.product, 2, PACK_CARTON)
        _sell(self.product, 5, PACK_PIECE)

    def test_the_stock_card_reads_in_packs_and_ends_on_the_shelf_figure(self):
        response = self.client.get(reverse("report_stock_card"), {"product": self.product.id, "all": "1"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10 Ctn")
        self.assertContains(response, "7 Ctn 11 Box 7 pcs")
        self.assertContains(response, "per Ctn")

    def test_stock_valuation_prices_the_carton_and_values_the_pieces(self):
        report = build_stock_valuation(None, None, {})
        row = report.rows[0]

        self.assertEqual(row["quantity"], Decimal("1147"))
        self.assertEqual(row["average_cost"], Decimal("1000.00"))
        self.assertEqual(row["sale_price"], Decimal("1250.00"))
        self.assertEqual(row["cost_value"], Decimal("7965.28"))
        self.assertEqual(row["packer"](row["quantity"]), "7 Ctn 11 Box 7 pcs")

    def test_sales_by_product_counts_pieces_and_keeps_the_money_exact(self):
        report = build_sales_by_product(None, None, {})
        row = report.rows[0]

        self.assertEqual(row["sold_qty"], Decimal("293"))
        self.assertEqual(row["net_sales"], Decimal("2543.40"))
        self.assertEqual(row["cost"], Decimal("2034.72"))


class ReturnInPacksTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.product = _biscuits()
        self.purchase_item = _buy(self.product, 10, "1000.00", "1250.00")

    def test_pieces_can_come_back_from_a_carton_sale(self):
        item = _sell(self.product, 2, PACK_CARTON)

        sale_return = create_sale_return(
            sale=item.sale,
            quantities={item.id: "36"},
            units={item.id: PACK_PIECE},
            refund_method="cash",
            reason="Damaged",
            actor=self.admin_user,
        )
        self.product.refresh_from_db()
        line = sale_return.items.get()

        # 36 pieces is a quarter of a carton.
        self.assertEqual(line.line_amount, Decimal("312.50"))
        self.assertEqual(line.cost_amount, Decimal("250.00"))
        self.assertEqual(line.unit_price, Decimal("8.68"))
        self.assertEqual(self.product.stock, Decimal("1440") - Decimal("288") + Decimal("36"))
        self.assertEqual(return_row(item)["returnable_display"], "1 Ctn 9 Box")

    def test_a_return_defaults_to_the_unit_of_the_line(self):
        item = _sell(self.product, 2, PACK_CARTON)

        sale_return = create_sale_return(
            sale=item.sale, quantities={item.id: "1"}, refund_method="cash", reason="Not needed", actor=self.admin_user
        )
        self.product.refresh_from_db()

        self.assertEqual(sale_return.total_amount, Decimal("1250.00"))
        self.assertEqual(sale_return.cost_amount, Decimal("1000.00"))
        self.assertEqual(self.product.stock, Decimal("1440") - Decimal("144"))

    def test_no_more_can_come_back_than_was_sold(self):
        item = _sell(self.product, 1, PACK_CARTON)

        with self.assertRaises(ValueError) as caught:
            create_sale_return(
                sale=item.sale, quantities={item.id: "13"}, units={item.id: PACK_BOX},
                refund_method="cash", reason="Too many", actor=self.admin_user,
            )

        self.assertIn("1 Ctn", str(caught.exception))

    def test_goods_cannot_come_back_in_a_larger_unit_than_they_were_sold_in(self):
        item = _sell(self.product, 24, PACK_BOX)

        with self.assertRaises(ValueError):
            create_sale_return(
                sale=item.sale, quantities={item.id: "1"}, units={item.id: PACK_CARTON},
                refund_method="cash", reason="Wrong unit", actor=self.admin_user,
            )

    def test_boxes_go_back_to_the_supplier_from_a_carton_purchase(self):
        purchase_return = create_purchase_return(
            purchase=self.purchase_item.purchase,
            quantities={self.purchase_item.id: "6"},
            units={self.purchase_item.id: PACK_BOX},
            settlement_method="cash",
            reason="Short dated",
            actor=self.admin_user,
        )
        self.product.refresh_from_db()

        self.assertEqual(purchase_return.total_amount, Decimal("500.00"))
        self.assertEqual(self.product.stock, Decimal("1440") - Decimal("72"))


class TillInPacksTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.product = _biscuits(barcode="555001")
        _buy(self.product, 10, "1000.00", "1250.00")

    def till(self, action, data=None):
        return self.client.post(reverse("till_action", kwargs={"action": action}), data or {}, **AJAX)

    def cart(self):
        return self.client.session.get("cart", {})

    def test_a_product_goes_on_the_bill_by_the_box(self):
        response = self.till("add", {"product_id": self.product.id})
        line = response.json()["lines"][0]

        self.assertEqual(line["unit_level"], PACK_BOX)
        self.assertEqual(line["unit"], "Box")
        self.assertEqual(line["rate"], "104.17")
        self.assertEqual([unit["code"] for unit in line["units"]], [PACK_CARTON, PACK_BOX, PACK_PIECE])
        self.assertEqual(self.cart()[str(self.product.id)]["unit"], PACK_BOX)

    def test_the_default_unit_is_a_setting(self):
        settings_row = SystemSettings.load()
        settings_row.till_default_unit = PACK_CARTON
        settings_row.save()

        line = self.till("add", {"product_id": self.product.id}).json()["lines"][0]

        self.assertEqual(line["unit_level"], PACK_CARTON)
        self.assertEqual(line["rate"], "1250.00")

    def test_the_unit_is_changed_on_the_line(self):
        self.till("add", {"product_id": self.product.id})
        self.till("quantity", {"product_id": self.product.id, "quantity": "3"})

        payload = self.till("unit", {"product_id": self.product.id, "unit": PACK_CARTON}).json()
        line = payload["lines"][0]

        self.assertTrue(payload["ok"])
        self.assertEqual(line["unit"], "Ctn")
        self.assertEqual(line["quantity"], "3")
        self.assertEqual(line["rate"], "1250.00")
        self.assertEqual(line["amount"], "3750.00")

    def test_a_unit_the_shelf_cannot_cover_is_refused(self):
        self.till("add", {"product_id": self.product.id})
        self.till("quantity", {"product_id": self.product.id, "quantity": "11"})

        payload = self.till("unit", {"product_id": self.product.id, "unit": PACK_CARTON}).json()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["lines"][0]["unit_level"], PACK_BOX)

    def test_a_typed_rate_is_carried_to_the_new_unit(self):
        self.till("add", {"product_id": self.product.id})
        self.till("price", {"product_id": self.product.id, "custom_price": "100.00"})

        line = self.till("unit", {"product_id": self.product.id, "unit": PACK_CARTON}).json()["lines"][0]

        self.assertEqual(line["rate"], "1200.00")

    def test_less_than_a_box_left_goes_on_by_the_piece(self):
        Product.objects.filter(pk=self.product.pk).update(stock=Decimal("5"))

        line = self.till("add", {"product_id": self.product.id}).json()["lines"][0]

        self.assertEqual(line["unit_level"], PACK_PIECE)

    def test_a_quantity_can_be_typed_with_its_unit(self):
        payload = self.till("scan", {"code": "555001", "quantity": "2", "quantity_unit": "ctn"}).json()
        line = payload["lines"][0]

        self.assertTrue(payload["ok"])
        self.assertEqual(line["unit_level"], PACK_CARTON)
        self.assertEqual(line["quantity"], "2")

    def test_checkout_records_the_unit_and_moves_pieces(self):
        self.till("add", {"product_id": self.product.id})
        self.till("quantity", {"product_id": self.product.id, "quantity": "2"})
        self.till("unit", {"product_id": self.product.id, "unit": PACK_CARTON})

        response = self.client.post(reverse("pos_screen"), {"action": "checkout", "payment_method": "cash"})
        item = SaleItem.objects.get()
        self.product.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(item.unit_level, PACK_CARTON)
        self.assertEqual(item.quantity, Decimal("2"))
        self.assertEqual(item.sale.total_amount, Decimal("2500.00"))
        self.assertEqual(item.line_cost, Decimal("2000.00"))
        self.assertEqual(self.product.stock, Decimal("1152"))

    def test_a_plain_product_has_no_unit_choice(self):
        plain = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("10"), price=Decimal("25"), stock=5)

        line = self.till("add", {"product_id": plain.id}).json()["lines"][0]

        self.assertEqual(line["units"], [])
        self.assertEqual(line["unit_level"], PACK_PIECE)
        self.assertEqual(seed_cart_item(plain)["price"], "25.00")
