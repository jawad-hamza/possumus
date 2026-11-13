"""Price lists: which rate a customer buys at, re-pricing the till when the customer changes,
the minimum margin, sales order lines and the price list screens."""
from decimal import Decimal

from django.urls import reverse

from pos.models import Customer, SaleItem
from pricing.models import PriceList, PriceListItem
from pricing.services.rates import RateBook, customer_rate

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, PricingCase


class ResolutionOrderTests(PricingCase):
    def test_customer_list_then_route_list_then_product_price(self):
        route_list = self.price_list("Route rates", (self.soap, 1400), (self.biscuit, 28))
        own_list = self.price_list("Ali rates", (self.soap, 1380))
        self.route.price_list = route_list
        self.route.save()

        # Before Ali has his own list he buys at the route's rates.
        self.assertEqual(customer_rate(self.soap, PACK_CARTON, self.shop), Decimal("1400.00"))
        self.shop.price_list = own_list
        self.shop.save()
        shop = Customer.objects.get(pk=self.shop.pk)
        book = RateBook(shop)
        self.assertEqual(book.rate(self.soap, PACK_CARTON), Decimal("1380.00"))
        # One box is a tenth of the carton; the piece price keeps six places.
        self.assertEqual(book.rate(self.soap, PACK_BOX), Decimal("138.00"))
        self.assertEqual(book.rate(self.soap, PACK_PIECE), Decimal("11.50"))
        # Biscuits are not on his own list: the route's list prices them.
        self.assertEqual(book.rate(self.biscuit, PACK_PIECE), Decimal("28.00"))
        # A customer on another route, with no list, pays the product price; so does a walk-in.
        self.assertEqual(customer_rate(self.soap, PACK_CARTON, self.other_shop), Decimal("1440.00"))
        self.assertEqual(customer_rate(self.soap, PACK_CARTON, None), Decimal("1440.00"))

    def test_a_switched_off_list_or_module_falls_through(self):
        own_list = self.price_list("Ali rates", (self.soap, 1380), active=False)
        route_list = self.price_list("Route rates", (self.soap, 1400))
        self.shop.price_list = own_list
        self.shop.save()
        self.route.price_list = route_list
        self.route.save()
        shop = Customer.objects.get(pk=self.shop.pk)
        self.assertEqual(customer_rate(self.soap, PACK_CARTON, shop), Decimal("1400.00"))

        self.module("price_lists", False)
        self.assertEqual(customer_rate(self.soap, PACK_CARTON, Customer.objects.get(pk=self.shop.pk)), Decimal("1440.00"))


class TillPriceListTests(PricingCase):
    def setUp(self):
        super().setUp()
        self.list = self.price_list("Wholesale", (self.soap, 1380))
        self.shop.price_list = self.list
        self.shop.save()

    def line(self):
        return self.cart()[str(self.soap.id)]

    def test_a_line_seeded_for_a_customer_takes_the_list_rate(self):
        self.add_to_bill(self.soap, 2, "ctn", customer=self.shop)

        line = self.line()
        self.assertEqual((line["price"], line["original_price"]), ("1380.00", "1380.00"))

        response = self.checkout(customer=self.shop)
        self.assertEqual(response.status_code, 302)
        item = SaleItem.objects.get()
        self.assertEqual((item.price, item.original_price), (Decimal("1380.00"), Decimal("1380.00")))
        self.assertEqual(self.last_sale().total_amount, Decimal("2760.00"))

    def test_changing_the_customer_reprices_lines_not_edited_by_hand(self):
        self.add_to_bill(self.soap, 2, "ctn")
        self.add_to_bill(self.biscuit, 3, "pcs")
        self.assertEqual(self.line()["price"], "1440.00")
        biscuit_list = PriceListItem.objects.create(price_list=self.list, product=self.biscuit, price=Decimal("27"))
        # The cashier types his own rate for the biscuits.
        self.till("price", product_id=self.biscuit.id, custom_price="29")

        payload = self.till("context", customer_id=self.shop.id)

        self.assertTrue(payload["ok"])
        self.assertEqual(self.line()["price"], "1380.00")
        biscuit = self.cart()[str(self.biscuit.id)]
        self.assertEqual(biscuit["price"], "29.00")  # typed by hand: kept
        self.assertEqual(biscuit["original_price"], "27.00")  # but the list rate is now the reference
        self.assertTrue(biscuit_list.pk)

        # Back to a walk-in: the soap line returns to the product price.
        self.till("context", customer_id="")
        self.assertEqual(self.line()["price"], "1440.00")

    def test_a_unit_change_takes_the_list_rate_of_the_new_unit(self):
        self.add_to_bill(self.soap, 2, "ctn", customer=self.shop)
        self.till("unit", product_id=self.soap.id, unit=PACK_BOX, customer_id=self.shop.id)
        line = self.line()
        self.assertEqual((line["unit"], line["price"]), (PACK_BOX, "138.00"))

    def test_a_list_rate_below_the_minimum_margin_blocks_the_sale(self):
        self.soap.minimum_margin_percent = Decimal("20")  # 1,200 cost: lowest carton price 1,440
        self.soap.save()
        payload = self.add_to_bill(self.soap, 1, "ctn", customer=self.shop)
        self.assertTrue(payload["checkout_disabled"])
        self.assertIn("cannot go below", payload["lines"][0]["pricing_error"])
        self.checkout(customer=self.shop)
        self.assertFalse(SaleItem.objects.exists())


class OrderPriceListTests(PricingCase):
    def test_customer_terms_carry_the_list_rates_and_the_invoice_shows_them(self):
        price_list = self.price_list("Wholesale", (self.soap, 1380))
        self.shop.price_list = price_list
        self.shop.save()
        terms = self.client.get(reverse("distribution_customer_terms", args=[self.shop.id])).json()["customer"]
        self.assertEqual(terms["price_list"], "Wholesale")
        self.assertEqual(terms["rates"][str(self.soap.id)][PACK_CARTON], "1380.00")
        self.assertEqual(terms["rates"][str(self.soap.id)][PACK_BOX], "138.00")

        from distribution.services.orders import invoice_order

        order = self.book(self.shop, (self.soap, 2, PACK_CARTON, 1380))
        result = invoice_order(order, user=self.admin_user)
        self.assertTrue(result.ok, result.error)
        item = result.sale.items.get()
        self.assertEqual((item.price, item.original_price, item.rate_note), (Decimal("1380.00"), Decimal("1380.00"), ""))


class PriceListScreenTests(PricingCase):
    def test_add_set_prices_fill_and_assign(self):
        response = self.client.post(reverse("pricing_price_list_add"), {"name": "Retailers", "is_active": "on"})
        price_list = PriceList.objects.get()
        self.assertRedirects(response, reverse("pricing_price_list_detail", args=[price_list.id]), fetch_redirect_response=False)
        url = reverse("pricing_price_list_detail", args=[price_list.id])

        # Typed per carton, kept per piece.
        self.client.post(url, {"action": "set_price", "item-product": self.soap.id, "item-price": "1416"})
        self.assertEqual(PriceListItem.objects.get(product=self.soap).price, Decimal("11.800000"))

        # Below the minimum margin is refused.
        self.soap.minimum_margin_percent = Decimal("10")
        self.soap.save()
        refused = self.client.post(url, {"action": "set_price", "item-product": self.soap.id, "item-price": "1300"})
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(PriceListItem.objects.get(product=self.soap).price, Decimal("11.800000"))

        # Fill the rest from product prices, 5% under.
        self.client.post(url, {"action": "fill", "fill-adjust_percent": "-5", "fill-category": ""})
        self.assertEqual(PriceListItem.objects.get(product=self.biscuit).price, Decimal("28.50"))
        # Soap was already on the list and replace was not ticked.
        self.assertEqual(PriceListItem.objects.get(product=self.soap).price, Decimal("11.800000"))

        self.client.post(url, {"action": "assign", "assign-customers": [self.shop.id], "assign-routes": [self.other_route.id]})
        self.refresh(self.shop, self.other_route)
        self.assertEqual((self.shop.price_list, self.other_route.price_list), (price_list, price_list))
        # Saving without Ali takes him off the list.
        self.client.post(url, {"action": "assign", "assign-routes": [self.other_route.id]})
        self.refresh(self.shop)
        self.assertIsNone(self.shop.price_list)

        page = self.client.get(url)
        self.assertContains(page, "Biscuit")
        listing = self.client.get(reverse("pricing_price_list_list"))
        self.assertContains(listing, "Retailers")

    def test_retail_price_is_typed_per_largest_pack_on_the_product_form(self):
        from pos.forms import ProductForm
        from pos.test_packaging_products_ui import _biscuits, _edit_payload

        product = _biscuits()  # 144 pieces to the carton
        response = self.client.post(reverse("product_edit", args=[product.id]), _edit_payload(product, retail_price="1500"))
        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.retail_price, Decimal("10.416667"))
        self.assertEqual(product.pack_retail_price(PACK_CARTON), Decimal("1500.00"))
        # Saved again untouched, the piece figure is not divided a second time.
        self.client.post(reverse("product_edit", args=[product.id]), _edit_payload(product, retail_price="1500.00"))
        product.refresh_from_db()
        self.assertEqual(product.retail_price, Decimal("10.416667"))

        # The invoice shows the MRP column for it.
        product.stock = Decimal("144")
        product.save()
        self.add_to_bill(product, 1, "ctn")
        self.checkout()
        invoice = self.client.get(reverse("invoice", args=[self.last_sale().id]))
        self.assertContains(invoice, ">MRP<")

        self.module("price_lists", False)
        self.assertNotIn("retail_price", ProductForm(instance=product).fields)
