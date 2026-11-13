"""Sales orders: booking, the customer box, turning orders into invoices singly and in bulk."""
from decimal import Decimal

from django.urls import reverse

from accounts.models import AuditLog
from pos.models import Sale, SaleItem, SystemSettings

from distribution.models import SalesOrder
from distribution.services.orders import invoice_order

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, DistributionCase


class OrderBookingTests(DistributionCase):
    def post_order(self, **extra):
        data = {
            "customer": self.shop.id,
            "order_date": self.today.isoformat(),
            "line_product": [self.soap.id, self.biscuit.id],
            "line_quantity": ["2", "5"],
            "line_unit": [PACK_CARTON, PACK_PIECE],
            "line_rate": ["1440", "30"],
            "line_discount": ["0", "10"],
        }
        data.update(extra)
        return self.client.post(reverse("distribution_order_add"), data)

    def test_order_is_booked_with_packs_and_takes_the_customers_route_and_booker(self):
        response = self.post_order()

        self.assertEqual(response.status_code, 302)
        order = SalesOrder.objects.get()
        self.assertEqual((order.route, order.order_booker, order.status), (self.route, self.booker, SalesOrder.BOOKED))
        soap, biscuit = order.items.order_by("id")
        self.assertEqual((soap.unit_level, soap.unit_factor, soap.base_quantity), (PACK_CARTON, Decimal("120"), Decimal("240")))
        self.assertEqual(biscuit.discount_percent, Decimal("10"))
        # 2 x 1,440 + 5 x 27
        self.assertEqual(order.total_amount, Decimal("3015.00"))
        # Nothing moves until it is invoiced.
        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("600"))
        self.assertFalse(Sale.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="sales_order_booked").exists())

    def test_order_form_and_customer_box_render(self):
        self.shop.credit_limit = Decimal("5000")
        self.shop.save()
        page = self.client.get(reverse("distribution_order_add"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="distProducts"')
        terms = self.client.get(reverse("distribution_customer_terms", args=[self.shop.id])).json()["customer"]
        self.assertEqual((terms["route_id"], terms["order_booker_id"], terms["has_limit"]), (str(self.route.id), str(self.booker.id), True))

    def test_a_rate_below_the_minimum_margin_is_refused_at_booking(self):
        self.soap.minimum_margin_percent = Decimal("10")
        self.soap.save()
        response = self.post_order(line_rate=["1200", "30"])  # cost of a carton is 1,200

        self.assertEqual(response.status_code, 200)
        self.assertFalse(SalesOrder.objects.exists())
        self.assertTrue(any("cannot go below" in message for message in self.messages_of(response)))

    def test_a_line_without_quantity_is_refused_and_typing_is_kept(self):
        response = self.post_order(line_quantity=["", "5"])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SalesOrder.objects.exists())
        self.assertEqual(response.context["lines"][1]["quantity"], "5")

    def test_edit_replaces_lines_and_cancel_needs_a_reason(self):
        order = self.book(self.shop, (self.soap, 1, PACK_CARTON))
        self.client.post(
            reverse("distribution_order_edit", args=[order.id]),
            {
                "customer": self.shop.id, "order_date": self.today.isoformat(), "route": self.route.id,
                "order_booker": self.booker.id, "line_product": [self.soap.id], "line_quantity": ["3"],
                "line_unit": [PACK_BOX], "line_rate": ["144"], "line_discount": ["0"],
            },
        )
        item = order.items.get()
        self.assertEqual((item.quantity, item.unit_level), (Decimal("3.000"), PACK_BOX))

        self.client.post(reverse("distribution_order_cancel", args=[order.id]), {"reason": ""})
        self.refresh(order)
        self.assertEqual(order.status, SalesOrder.BOOKED)
        self.client.post(reverse("distribution_order_cancel", args=[order.id]), {"reason": "Shop closed for a week"})
        self.refresh(order)
        self.assertEqual((order.status, order.cancel_reason), (SalesOrder.CANCELLED, "Shop closed for a week"))
        # A cancelled order is never invoiced.
        self.assertFalse(invoice_order(order, user=self.admin_user).ok)

    def test_list_filters_by_status_route_and_booker(self):
        self.book(self.shop, (self.soap, 1, PACK_CARTON))
        self.book(self.other_shop, (self.biscuit, 2, PACK_PIECE))
        everything = self.client.get(reverse("distribution_order_list"))
        self.assertEqual(len(everything.context["orders"]), 2)
        on_route = self.client.get(reverse("distribution_order_list"), {"route": self.route.id})
        self.assertEqual([order.customer for order in on_route.context["orders"]], [self.shop])
        by_booker = self.client.get(reverse("distribution_order_list"), {"order_booker": self.booker.id})
        self.assertEqual(len(by_booker.context["orders"]), 1)
        invoiced = self.client.get(reverse("distribution_order_list"), {"status": "invoiced"})
        self.assertEqual(len(invoiced.context["orders"]), 0)


class OrderToInvoiceTests(DistributionCase):
    def test_order_becomes_a_credit_invoice_through_the_till_rules(self):
        order = self.book(self.shop, (self.soap, 2, PACK_CARTON), (self.biscuit, 5, PACK_PIECE, 30, 10))

        response = self.client.post(reverse("distribution_order_invoice", args=[order.id]), {"payment": "credit"}, follow=True)

        self.refresh(order, self.soap, self.biscuit)
        sale = order.sale
        self.assertEqual(order.status, SalesOrder.INVOICED)
        self.assertTrue(sale.is_credit)
        self.assertEqual((sale.customer, sale.order_booker, sale.sold_by), (self.shop, self.booker, self.admin_user))
        self.assertTrue(sale.invoice_number)
        self.assertEqual(sale.total_amount, Decimal("3015.00"))
        line = sale.items.get(product=self.soap)
        self.assertEqual((line.quantity, line.unit_level, line.base_quantity), (Decimal("2.000"), PACK_CARTON, Decimal("240.000")))
        self.assertEqual(self.soap.stock, Decimal("360"))
        self.assertEqual(self.biscuit.stock, Decimal("45"))
        self.assertEqual(self.balance(self.shop), Decimal("3015.00"))
        self.assertEqual(self.account_balance("Accounts Receivable"), Decimal("3015.00"))
        self.assertEqual(self.account_balance("Cost of Goods Sold"), Decimal("2500.00"))
        self.assert_books_balance()
        self.assertTrue(all(item.short_quantity == 0 for item in order.items.all()))
        self.assertTrue(any("1 invoice made" in text for text in self.messages_of(response)))

    def test_cash_invoice_is_paid_into_cash(self):
        order = self.book(self.shop, (self.biscuit, 2, PACK_PIECE))
        result = invoice_order(order, user=self.admin_user, credit=False)
        self.assertTrue(result.ok)
        self.assertFalse(result.sale.is_credit)
        self.assertEqual(result.sale.paid_amount, Decimal("60.00"))
        self.assertEqual(self.account_balance("Cash"), Decimal("60.00"))
        self.assertEqual(self.balance(self.shop), Decimal("0"))

    def test_short_stock_is_invoiced_partly_and_the_shortage_stays_on_the_order(self):
        # 600 pieces = 5 cartons on the shelf; two lines of soap share it.
        order = self.book(self.shop, (self.soap, 4, PACK_CARTON), (self.soap, 15, PACK_BOX), (self.biscuit, 60, PACK_PIECE))

        result = invoice_order(order, user=self.admin_user)

        self.assertTrue(result.ok, result.error)
        cartons, boxes, biscuits = order.items.order_by("id")
        self.assertEqual((cartons.invoiced_quantity, cartons.short_quantity), (Decimal("4.000"), Decimal("0")))
        # 120 pieces left = 10 boxes of the 15 ordered.
        self.assertEqual((boxes.invoiced_quantity, boxes.short_quantity), (Decimal("10.000"), Decimal("5.000")))
        self.assertEqual((biscuits.invoiced_quantity, biscuits.short_quantity), (Decimal("50.000"), Decimal("10.000")))
        self.refresh(self.soap, self.biscuit, order)
        self.assertEqual((self.soap.stock, self.biscuit.stock), (Decimal("0"), Decimal("0")))
        self.assertEqual(len(result.shortages), 2)
        self.assertIn("short 5", result.shortages[0].sentence())
        self.assertTrue(order.has_shortage)
        # Short value: 5 boxes x 144 + 10 biscuits x 30
        self.assertEqual(order.short_amount, Decimal("1020.00"))
        self.assertEqual(order.sale.total_amount, order.invoiced_amount)

    def test_nothing_in_stock_makes_no_invoice(self):
        self.soap.stock = Decimal("100")  # less than one carton
        self.soap.save()
        order = self.book(self.shop, (self.soap, 1, PACK_CARTON))

        result = invoice_order(order, user=self.admin_user)

        self.assertFalse(result.ok)
        self.assertIn("Nothing on it is in stock", result.error)
        self.refresh(order)
        self.assertEqual(order.status, SalesOrder.BOOKED)
        self.assertFalse(Sale.objects.exists())

    def test_credit_limit_refuses_and_nothing_is_saved(self):
        self.shop.credit_limit = Decimal("1000")
        self.shop.save()
        order = self.book(self.shop, (self.soap, 1, PACK_CARTON))
        number_before = SystemSettings.load().next_invoice_sequence

        result = invoice_order(order, user=self.admin_user)

        self.assertFalse(result.ok)
        self.assertIn("Credit sale refused", result.error)
        self.assertIn("credit limit", result.error)
        self.refresh(order, self.soap)
        self.assertEqual((order.status, self.soap.stock), (SalesOrder.BOOKED, Decimal("600")))
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(SaleItem.objects.exists())
        self.assertEqual(SystemSettings.load().next_invoice_sequence, number_before)

    def test_credit_limit_set_to_warn_invoices_with_a_warning(self):
        self.settings_row(credit_limit_action=SystemSettings.CREDIT_LIMIT_WARN)
        self.shop.credit_limit = Decimal("1000")
        self.shop.save()
        order = self.book(self.shop, (self.soap, 1, PACK_CARTON))

        result = invoice_order(order, user=self.admin_user)

        self.assertTrue(result.ok)
        self.assertTrue(any("over the credit limit" in warning for warning in result.warnings))

    def test_price_override_off_puts_the_price_list_rate_on_the_invoice(self):
        self.settings_row(allow_price_override=False)
        order = self.book(self.shop, (self.biscuit, 2, PACK_PIECE, 35))
        result = invoice_order(order, user=self.admin_user)
        self.assertEqual(result.sale.items.get().price, Decimal("30.00"))
        self.assertTrue(result.warnings)


class BulkInvoiceTests(DistributionCase):
    def test_ticked_orders_become_invoices_and_a_refusal_does_not_stop_the_rest(self):
        first = self.book(self.shop, (self.soap, 1, PACK_CARTON))
        second = self.book(self.other_shop, (self.biscuit, 10, PACK_PIECE))
        self.other_shop.credit_limit = Decimal("100")
        self.other_shop.save()
        third = self.book(self.other_shop, (self.biscuit, 1, PACK_PIECE))

        response = self.client.post(
            reverse("distribution_order_bulk_invoice"),
            {"order": [first.id, second.id, third.id], "payment": "credit"},
            follow=True,
        )

        self.refresh(first, second, third)
        self.assertEqual(first.status, SalesOrder.INVOICED)
        self.assertEqual(second.status, SalesOrder.BOOKED)  # 300 over a limit of 100
        self.assertEqual(third.status, SalesOrder.INVOICED)
        self.assertEqual(Sale.objects.count(), 2)
        texts = self.messages_of(response)
        self.assertTrue(any("2 invoices made" in text for text in texts))
        self.assertTrue(any(second.display_number in text and "not invoiced" in text for text in texts))
        self.assert_books_balance()

    def test_bulk_needs_a_tick(self):
        response = self.client.post(reverse("distribution_order_bulk_invoice"), {}, follow=True)
        self.assertTrue(any("Tick the booked orders" in text for text in self.messages_of(response)))
