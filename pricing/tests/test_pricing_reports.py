"""The Scheme Usage and Price Lists reports, their CSV, and the modules switched off."""
import csv
import io
from decimal import Decimal

from django.urls import reverse

from distribution.services.orders import invoice_order
from pos.services.reporting import report_navigation
from pricing.models import Scheme

from .base import PACK_CARTON, PricingCase


def read_csv(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


class SchemeUsageReportTests(PricingCase):
    def setUp(self):
        super().setUp()
        self.free = self.scheme("Soap 10+1")
        self.percent = self.scheme(
            "Biscuit 10% from 10", product=self.biscuit, reward=Scheme.REWARD_PERCENT,
            trigger_quantity=Decimal("10"), trigger_unit="piece", discount_percent=Decimal("10"),
        )
        self.assertTrue(invoice_order(self.book(self.shop, (self.soap, 10, PACK_CARTON)), user=self.admin_user).ok)
        self.assertTrue(invoice_order(self.book(self.other_shop, (self.biscuit, 10, "piece")), user=self.admin_user).ok)

    def report(self, **params):
        return self.client.get(reverse("report_scheme_usage"), params)

    def test_by_scheme_shows_free_goods_at_cost_and_discounts(self):
        response = self.report()
        self.assertEqual(response.status_code, 200)
        rows = {row["group"]: row for row in response.context["report"].rows}
        self.assertEqual(rows["Soap 10+1"]["free_cost"], Decimal("1200.00"))
        self.assertEqual(rows["Soap 10+1"]["discount"], Decimal("0.00"))
        self.assertEqual(rows["Biscuit 10% from 10"]["discount"], Decimal("30.00"))
        self.assertEqual(response.context["report"].totals["total"], Decimal("1230.00"))

    def test_by_customer_route_and_product_and_csv(self):
        by_customer = {row["group"]: row["total"] for row in self.report(by="customer").context["report"].rows}
        self.assertEqual(by_customer, {"Ali Store": Decimal("1200.00"), "Bilal Mart": Decimal("30.00")})
        by_route = {row["group"]: row["total"] for row in self.report(by="route").context["report"].rows}
        self.assertEqual(by_route["Main Bazaar (Mirpur)"], Decimal("1200.00"))
        by_product = self.report(by="product").context["report"].rows
        self.assertEqual([(row["group"], row["free_base"]) for row in by_product], [("Soap", Decimal("120.000"))])

        export = self.report(export="csv")
        lines = read_csv(export)
        self.assertEqual(lines[0][0], "Scheme")
        self.assertIn("Total", lines[-1][0])


class PriceListReportTests(PricingCase):
    def test_lists_every_price_and_flags_one_below_the_minimum_margin(self):
        self.price_list("Wholesale", (self.soap, 1380), (self.biscuit, 25))
        self.biscuit.minimum_margin_percent = Decimal("30")  # lowest 26 on a cost of 20
        self.biscuit.save()
        response = self.client.get(reverse("report_price_lists"))
        rows = {row["product"]: row for row in response.context["report"].rows}
        self.assertEqual(rows["Soap"]["list_price"], Decimal("1380.00"))
        self.assertEqual(rows["Soap"]["change"], Decimal("-4.17"))
        self.assertEqual(rows["Biscuit"]["check"], "Below minimum margin")
        export = read_csv(self.client.get(reverse("report_price_lists"), {"export": "csv"}))
        self.assertEqual(export[0][0], "Price list")
        self.assertEqual(len(export), 3)


class ModulesOffTests(PricingCase):
    def test_price_lists_off_hides_screens_report_and_rates(self):
        price_list = self.price_list("Wholesale", (self.soap, 1380))
        self.shop.price_list = price_list
        self.shop.save()
        self.module("price_lists", False)

        self.assertEqual(self.client.get(reverse("pricing_price_list_list")).status_code, 302)
        self.assertEqual(self.client.get(reverse("report_price_lists")).status_code, 302)
        slugs = {link["slug"] for group in report_navigation() for link in group["links"]}
        self.assertNotIn("price-lists", slugs)
        self.assertIn("scheme-usage", slugs)
        payload = self.add_to_bill(self.soap, 1, "ctn", customer=self.shop)
        self.assertEqual(payload["lines"][0]["rate"], "1440.00")
        page = self.client.get(reverse("product_list"))
        self.assertNotContains(page, reverse("pricing_price_list_list"))
        self.assertContains(page, reverse("pricing_scheme_list"))

    def test_schemes_off_hides_the_usage_report_and_screens(self):
        self.module("schemes", False)
        self.assertEqual(self.client.get(reverse("report_scheme_usage")).status_code, 302)
        self.assertEqual(self.client.get(reverse("pricing_scheme_add")).status_code, 302)
        slugs = {link["slug"] for group in report_navigation() for link in group["links"]}
        self.assertNotIn("scheme-usage", slugs)

    def test_pages_render(self):
        price_list = self.price_list("Wholesale", (self.soap, 1380))
        scheme = self.scheme("Soap 10+1")
        for url in (
            reverse("pricing_price_list_add"),
            reverse("pricing_price_list_edit", args=[price_list.id]),
            reverse("pricing_price_list_detail", args=[price_list.id]),
            reverse("pricing_scheme_add"),
            reverse("pricing_scheme_edit", args=[scheme.id]),
            reverse("pricing_scheme_list") + "?show=all",
            reverse("product_edit", args=[self.soap.id]),
            reverse("settings_page"),
        ):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse("product_edit", args=[self.soap.id])), "Retail price (MRP)")
        self.assertContains(self.client.get(reverse("settings_page")), "Cashier may take free goods off a bill")
        # The till page draws a free-goods line of its own.
        self.add_to_bill(self.soap, 10, "ctn")
        page = self.client.get(reverse("pos_screen"))
        self.assertContains(page, f'data-line="free-{self.soap.id}"')
        self.assertContains(page, "Price lists")

    def test_scheme_screens_add_edit_and_switch(self):
        response = self.client.post(
            reverse("pricing_scheme_add"),
            {
                "name": "Soap 10+1 June", "is_active": "on", "scope": "all", "product": self.soap.id,
                "trigger_quantity": "10", "trigger_unit": "carton", "reward": "free",
                "free_quantity": "1", "free_unit": "carton",
            },
        )
        self.assertEqual(response.status_code, 302)
        scheme = Scheme.objects.get()
        self.assertEqual(scheme.offer_label, "10 Ctn + 1 Ctn free")
        listing = self.client.get(reverse("pricing_scheme_list"))
        self.assertContains(listing, "Soap 10+1 June")
        self.client.post(reverse("pricing_scheme_toggle", args=[scheme.id]))
        scheme.refresh_from_db()
        self.assertFalse(scheme.is_active)
        bad = self.client.post(
            reverse("pricing_scheme_edit", args=[scheme.id]),
            {"name": "Soap", "scope": "customer", "product": self.soap.id, "trigger_quantity": "10",
             "trigger_unit": "carton", "reward": "percent"},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertTrue(bad.context["form"].errors)
