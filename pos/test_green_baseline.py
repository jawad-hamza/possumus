from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse

from pos.models import Brand, CompanyProfile, Product, Sale, SaleItem, SystemSettings
from pos.services import currency
from pos.services.business_rules import get_active_company_profile
from pos.test_support import AuthenticatedAdminTestCase
from pos.views.pos_views import _pdf_currency_label


def set_currency(symbol, code="USD"):
    settings_obj = SystemSettings.load()
    settings_obj.currency_symbol = symbol
    settings_obj.currency_code = code
    settings_obj.save()
    return settings_obj


class CurrencyServiceTests(TestCase):
    def test_format_money_follows_settings_and_tolerates_bad_input(self):
        set_currency("$")
        self.assertEqual(currency.format_money(Decimal("1250.5")), "$ 1,250.50")
        self.assertEqual(currency.format_money(None), "$ 0.00")
        self.assertEqual(currency.format_money("not a number"), "$ 0.00")

    def test_primed_symbol_is_used_then_dropped_on_settings_save(self):
        currency.prime_currency_symbol("AED")
        self.assertEqual(currency.format_money(5), "AED 5.00")
        set_currency("")
        self.assertEqual(currency.format_money(5), "5.00")

    def test_pdf_label_falls_back_to_code_for_symbols_the_font_cannot_draw(self):
        self.assertEqual(_pdf_currency_label(set_currency("₹", "INR")), "INR")
        self.assertEqual(_pdf_currency_label(set_currency("€", "EUR")), "€")
        self.assertEqual(_pdf_currency_label(set_currency("", "USD")), "")


class DefaultCompanyProfileTests(TestCase):
    @override_settings(PUBLIC_BRAND_NAME="ZehnMS")
    def test_created_profile_uses_the_brand_name(self):
        company = get_active_company_profile(create=True)
        self.assertEqual(company.name, "ZehnMS")
        self.assertEqual(CompanyProfile.objects.count(), 1)


class ViewRegressionTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.flour = Product.objects.create(
            name="Flour",
            sku="FLR-9",
            unit="kg",
            cost_price=Decimal("100.00"),
            price=Decimal("140.00"),
            stock=Decimal("12.500"),
        )

    def _sell_flour(self, quantity="0.500", total="70.00"):
        sale = Sale.objects.create(total_amount=Decimal(total), paid_amount=Decimal(total), sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.flour, quantity=Decimal(quantity), price=Decimal("140.00"))
        return sale

    def test_product_detail_page_totals_fractional_quantities(self):
        self._sell_flour("0.500")
        self._sell_flour("2.750", total="385.00")
        response = self.client.get(reverse("product_view", args=[self.flour.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_sold"], Decimal("3.250"))
        self.assertEqual(response.context["total_purchased"], Decimal("0"))
        self.assertContains(response, "3.25 kg")

    def test_product_edit_shows_low_stock_threshold_without_trailing_zeros(self):
        self.flour.low_stock_threshold = Decimal("2.500")
        self.flour.save()
        form = self.client.get(reverse("product_edit", args=[self.flour.id])).context["form"]
        self.assertEqual(form["low_stock_threshold"].value(), "2.5")

    def test_brand_add_page_renders_and_saves(self):
        self.assertEqual(self.client.get(reverse("brand_add")).status_code, 200)
        response = self.client.post(reverse("brand_add"), {"name": "Sunrise Mills", "is_active": "on"})
        self.assertRedirects(response, reverse("product_list"))
        self.assertTrue(Brand.objects.filter(name="Sunrise Mills", is_active=True).exists())

    def test_brand_add_shows_errors_instead_of_crashing(self):
        response = self.client.post(reverse("brand_add"), {"name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Brand.objects.exists())

    def test_quick_create_product_reply_carries_unit_and_pricing_rules(self):
        response = self.client.post(
            reverse("quick_create_product"),
            {
                "quick_product-name": "Sugar",
                "quick_product-sku": "SGR-1",
                "quick_product-unit": "kg",
                "quick_product-markup_percent": "25",
                "quick_product-minimum_margin_percent": "5",
                "quick_product-low_stock_threshold": "2",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        product = response.json()["product"]
        self.assertEqual(product["unit"], "kg")
        self.assertEqual(Decimal(product["markup_percent"]), Decimal("25"))
        self.assertEqual(Decimal(product["minimum_margin_percent"]), Decimal("5"))

    def test_receipt_pdf_renders_for_weighed_items_and_any_currency(self):
        sale = self._sell_flour()
        for symbol, code in (("$", "USD"), ("₹", "INR"), ("", "USD")):
            set_currency(symbol, code)
            response = self.client.get(reverse("invoice_pdf", args=[sale.id]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF"))

    def test_dashboard_and_reports_follow_the_configured_currency(self):
        set_currency("$")
        self._sell_flour("3.500", total="490.00")
        for url_name in ("dashboard", "reports_home"):
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "Rs ")
            self.assertContains(response, "3.5 kg")
        self.assertContains(self.client.get(reverse("dashboard")), "Avg invoice $ ")
        self.assertContains(self.client.get(reverse("reports_home")), "Gross profit $ ")
