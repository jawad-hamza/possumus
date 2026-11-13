"""Schemes at the till: free goods as zero lines that leave stock at cost, scheme discounts as the
line discount, the books staying balanced, the setting for taking free goods off, and the prints."""
from decimal import Decimal

from django.urls import reverse

from pos.models import SaleItem, SaleReturn, SystemSettings
from pos.services.returns import create_sale_return
from pricing.models import Scheme

from .base import PACK_CARTON, PricingCase


class FreeGoodsAtTheTillTests(PricingCase):
    def setUp(self):
        super().setUp()
        self.ten_plus_one = self.scheme("Soap 10+1")

    def test_free_goods_show_as_a_zero_line_and_leave_stock_at_cost(self):
        payload = self.add_to_bill(self.soap, 10, "ctn", customer=self.shop)

        lines = payload["lines"]
        self.assertEqual(len(lines), 2)
        free = lines[1]
        self.assertEqual((free["product_id"], free["is_free"], free["quantity"], free["amount"]), (f"free-{self.soap.id}", True, "1", "0.00"))
        self.assertEqual(free["scheme"], "Soap 10+1")
        self.assertEqual(payload["totals"]["total"], "14400.00")
        self.assertIn("Free, Soap 10+1", payload["html"]["bill"])

        self.checkout(customer=self.shop)

        sale = self.last_sale()
        self.assertEqual(sale.total_amount, Decimal("14400.00"))
        paid, given = sale.items.order_by("id")
        self.assertFalse(paid.is_free)
        self.assertEqual((given.is_free, given.price, given.quantity, given.unit_level), (True, Decimal("0"), Decimal("1"), PACK_CARTON))
        self.assertEqual((given.scheme, given.scheme_name), (self.ten_plus_one, "Soap 10+1"))
        self.assertEqual(given.cost_price, Decimal("1200"))
        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("2400") - 11 * 120)
        # Revenue is what was charged; the cost of all eleven cartons is Cost of Goods Sold.
        self.assertEqual(self.account_balance("Sales"), Decimal("-14400.00"))
        self.assertEqual(self.account_balance("Cost of Goods Sold"), Decimal("13200.00"))
        self.assertEqual(self.account_balance("Inventory"), Decimal("-13200.00"))
        self.assert_books_balance()

    def test_free_goods_get_only_what_the_paid_lines_leave_on_the_shelf(self):
        self.soap.stock = Decimal("1200")  # exactly 10 cartons
        self.soap.save()
        payload = self.add_to_bill(self.soap, 10, "ctn")
        free = payload["lines"][1]
        self.assertEqual(free["quantity"], "0")
        self.assertFalse(payload["checkout_disabled"])
        self.assertIn("short", payload["html"]["bill"])

        self.checkout()
        sale = self.last_sale()
        self.assertEqual(sale.items.count(), 1)
        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("0"))

    def test_free_goods_follow_the_paid_quantity(self):
        self.soap.stock = Decimal("4800")
        self.soap.save()
        self.add_to_bill(self.soap, 10, "ctn")
        payload = self.till("quantity", product_id=self.soap.id, quantity="20")
        self.assertEqual(payload["lines"][1]["quantity"], "2")
        payload = self.till("quantity", product_id=self.soap.id, quantity="9")
        self.assertEqual(len(payload["lines"]), 1)

    def test_taking_free_goods_off_needs_the_setting(self):
        self.add_to_bill(self.soap, 10, "ctn")
        refused = self.till("remove", product_id=f"free-{self.soap.id}")
        self.assertFalse(refused["ok"])
        self.assertEqual(len(refused["lines"]), 2)
        # Free lines are not edited like paid ones.
        self.assertFalse(self.till("increase", product_id=f"free-{self.soap.id}")["ok"])

        row = SystemSettings.load_raw()
        row.allow_remove_free_goods = True
        row.save()
        allowed = self.till("remove", product_id=f"free-{self.soap.id}")
        self.assertTrue(allowed["ok"])
        self.assertEqual(len(allowed["lines"]), 1)
        # It stays off while the same scheme gives it, even when the quantity grows.
        self.assertEqual(len(self.till("quantity", product_id=self.soap.id, quantity="20")["lines"]), 1)

    def test_free_goods_of_another_product(self):
        self.ten_plus_one.is_active = False
        self.ten_plus_one.save()
        self.scheme("Soap 10 + 5 biscuits", free_product=self.biscuit, free_quantity=Decimal("5"))
        payload = self.add_to_bill(self.soap, 10, "ctn")
        self.assertEqual(payload["lines"][1]["name"], "Biscuit")
        self.checkout()
        given = self.last_sale().items.get(is_free=True)
        self.assertEqual((given.product, given.quantity, given.cost_price), (self.biscuit, Decimal("5"), Decimal("20")))
        self.refresh(self.biscuit)
        self.assertEqual(self.biscuit.stock, Decimal("45"))
        self.assert_books_balance()

    def test_invoice_receipt_and_pdf_show_the_free_line(self):
        self.add_to_bill(self.soap, 10, "ctn")
        self.checkout()
        sale = self.last_sale()
        page = self.client.get(reverse("invoice", args=[sale.id]))
        self.assertContains(page, "Free, Soap 10+1")
        pdf = self.client.get(reverse("invoice_pdf", args=[sale.id]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_returning_free_goods_puts_back_only_their_cost(self):
        self.add_to_bill(self.soap, 10, "ctn")
        self.checkout()
        sale = self.last_sale()
        given = sale.items.get(is_free=True)
        sale_return = create_sale_return(
            sale=sale, quantities={given.id: "1"}, refund_method=SaleReturn.CASH, reason="Damaged", actor=self.admin_user
        )
        self.assertEqual((sale_return.total_amount, sale_return.cost_amount), (Decimal("0.00"), Decimal("1200.00")))
        self.assert_books_balance()


class SchemeDiscountAtTheTillTests(PricingCase):
    def test_a_percentage_scheme_is_the_line_discount_even_when_line_discounts_are_off(self):
        scheme = self.scheme("Soap 2% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("2"))
        row = SystemSettings.load_raw()
        row.allow_line_discount = False
        row.save()
        payload = self.add_to_bill(self.soap, 5, "ctn")
        line = payload["lines"][0]
        self.assertEqual((line["discount_percent"], line["scheme"], line["amount"]), ("2.00", "Soap 2% from 5", "7056.00"))

        self.checkout()
        item = SaleItem.objects.get()
        self.assertEqual((item.discount_percent, item.scheme, item.scheme_discount), (Decimal("2.00"), scheme, Decimal("144.00")))
        self.assertEqual(self.last_sale().total_amount, Decimal("7056.00"))
        self.assert_books_balance()

    def test_an_amount_scheme_lowers_the_rate_and_keeps_the_list_rate(self):
        self.scheme("Soap 40 off a ctn", reward=Scheme.REWARD_AMOUNT, trigger_quantity=Decimal("5"), discount_amount=Decimal("40"))
        row = SystemSettings.load_raw()
        row.allow_price_override = False
        row.save()
        payload = self.add_to_bill(self.soap, 5, "ctn")
        self.assertEqual(payload["lines"][0]["rate"], "1400.00")
        self.checkout()
        item = SaleItem.objects.get()
        self.assertEqual((item.price, item.original_price, item.scheme_discount, item.rate_note), (Decimal("1400.00"), Decimal("1440.00"), Decimal("200.00"), "Soap 40 off a ctn"))

    def test_dropping_below_the_trigger_takes_the_discount_away(self):
        self.scheme("Soap 2% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("2"))
        self.add_to_bill(self.soap, 5, "ctn")
        payload = self.till("quantity", product_id=self.soap.id, quantity="4")
        self.assertEqual(payload["lines"][0]["discount_percent"], "0.00")

    def test_a_discount_typed_by_hand_stands(self):
        self.scheme("Soap 2% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("2"))
        self.add_to_bill(self.soap, 5, "ctn")
        payload = self.till("price", product_id=self.soap.id, line_discount="1")
        self.assertEqual(payload["lines"][0]["discount_percent"], "1.00")
        self.assertEqual(payload["lines"][0]["scheme"], "")


class SchemesSwitchedOffTests(PricingCase):
    def test_no_free_goods_or_scheme_discount_with_the_module_off(self):
        self.scheme("Soap 10+1")
        self.scheme("Soap 2% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("2"))
        self.add_to_bill(self.soap, 10, "ctn")
        self.module("schemes", False)
        payload = self.till("context")
        self.assertEqual(len(payload["lines"]), 1)
        self.assertEqual(payload["lines"][0]["discount_percent"], "0.00")
        self.checkout()
        self.assertEqual(SaleItem.objects.filter(is_free=True).count(), 0)
        self.assertEqual(self.last_sale().total_amount, Decimal("14400.00"))
        self.assertEqual(self.client.get(reverse("pricing_scheme_list")).status_code, 302)
        self.assertNotContains(self.client.get(reverse("pos_screen")), reverse("pricing_scheme_list"))
