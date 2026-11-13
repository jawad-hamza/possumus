"""Booked orders turned into invoices get the customer's schemes on the quantity invoiced, and the
load sheet shows the free goods in their own column."""
from decimal import Decimal

from django.urls import reverse

from distribution.services.loads import add_invoices, create_load, load_sheet
from distribution.services.orders import invoice_order, invoice_orders
from pricing.models import Scheme

from .base import PACK_CARTON, PricingCase


class OrderToInvoiceSchemeTests(PricingCase):
    def test_free_goods_are_added_on_the_invoiced_quantity(self):
        scheme = self.scheme("Soap 10+1")
        order = self.book(self.shop, (self.soap, 12, PACK_CARTON), (self.biscuit, 5, "piece"))

        result = invoice_order(order, user=self.admin_user)

        self.assertTrue(result.ok, result.error)
        sale = result.sale
        self.assertEqual(sale.total_amount, Decimal("17430.00"))  # 12 x 1,440 + 5 x 30
        free = sale.items.get(is_free=True)
        self.assertEqual((free.product, free.quantity, free.unit_level, free.scheme), (self.soap, Decimal("1"), PACK_CARTON, scheme))
        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("2400") - 13 * 120)
        self.assertEqual(self.account_balance("Cost of Goods Sold"), Decimal("15700.00"))  # 13 x 1,200 + 5 x 20
        self.assert_books_balance()

    def test_short_free_goods_are_reported_and_the_paid_lines_come_first(self):
        self.scheme("Soap 10+1")
        self.soap.stock = Decimal("1200")
        self.soap.save()
        order = self.book(self.shop, (self.soap, 10, PACK_CARTON))
        result = invoice_order(order, user=self.admin_user)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.sale.items.filter(is_free=True).exists())
        self.assertTrue(any("Free goods short" in warning for warning in result.warnings))

    def test_a_shortage_on_the_paid_line_lowers_the_free_goods(self):
        self.scheme("Soap 10+1")
        self.soap.stock = Decimal("1320")  # 11 cartons
        self.soap.save()
        order = self.book(self.shop, (self.soap, 20, PACK_CARTON))
        result = invoice_order(order, user=self.admin_user)
        # 11 cartons can go: 11 paid (one free carton earned) but nothing is left for it.
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.sale.items.get(is_free=False).quantity, Decimal("11"))
        self.assertTrue(result.shortages)
        self.assertTrue(any("Free goods short" in warning for warning in result.warnings))

    def test_a_percentage_scheme_applies_when_the_order_line_has_no_discount_of_its_own(self):
        self.scheme("Soap 3% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("3"))
        plain = self.book(self.shop, (self.soap, 5, PACK_CARTON))
        typed = self.book(self.other_shop, (self.soap, 5, PACK_CARTON, 1440, 1))
        first, second = invoice_orders([plain, typed], user=self.admin_user)
        line = first.sale.items.get()
        self.assertEqual((line.discount_percent, line.scheme_name, line.scheme_discount), (Decimal("3.00"), "Soap 3% from 5", Decimal("216.00")))
        other = second.sale.items.get()
        self.assertEqual((other.discount_percent, other.scheme_name), (Decimal("1.00"), ""))

    def test_load_sheet_has_a_free_column(self):
        self.scheme("Soap 10+1")
        order = self.book(self.shop, (self.soap, 10, PACK_CARTON))
        sale = invoice_order(order, user=self.admin_user).sale
        load = create_load(load_date=self.today, route=self.route, delivery_man=self.driver, vehicle=self.van, user=self.admin_user)
        add_invoices(load, [sale.id], user=self.admin_user)

        sheet = load_sheet(load)
        line = sheet["lines"][0]
        self.assertEqual((line["cartons"], line["free"], sheet["show_free"]), (Decimal("10"), "1 Ctn", True))
        self.assertEqual(sheet["total_cartons"], Decimal("10"))
        printed = self.client.get(reverse("distribution_load_print", args=[load.id]))
        self.assertContains(printed, "<th scope=\"col\" class=\"num\">Free</th>", html=False)
        self.assertContains(printed, "1 Ctn")
