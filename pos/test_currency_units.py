from decimal import Decimal

from django.template import Context, Template
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from pos.forms import PurchaseItemForm, QuickProductForm, SystemSettingsForm
from pos.models import Product, Sale, SaleItem, SystemSettings
from pos.templatetags import currency_tags
from pos.test_support import AuthenticatedAdminTestCase


def render(source, **context):
    return Template("{% load currency_tags %}" + source).render(Context(context))


def set_currency(symbol, code="USD"):
    settings_obj = SystemSettings.load()
    settings_obj.currency_symbol = symbol
    settings_obj.currency_code = code
    settings_obj.save()
    return settings_obj


def cart_line(product, quantity):
    return {
        "name": product.name,
        "price": str(product.price),
        "original_price": str(product.price),
        "discount_percent": "0.00",
        "quantity": quantity,
    }


class CurrencyFilterTests(TestCase):
    def test_money_uses_default_symbol_with_thousands_separator(self):
        self.assertEqual(render("{{ value|money }}", value=Decimal("1250.5")), "Rs 1,250.50")
        self.assertEqual(render("{{ value|money }}", value=None), "Rs 0.00")
        self.assertEqual(render("{{ value|money }}", value="not a number"), "Rs 0.00")

    def test_money_follows_the_configured_symbol(self):
        set_currency("$")
        self.assertEqual(render("{{ value|money }}", value=40), "$ 40.00")
        self.assertEqual(render("{% currency_symbol %}"), "$")

    def test_blank_symbol_leaves_a_clean_amount(self):
        set_currency("")
        self.assertEqual(render("{{ value|money }}", value=40), "40.00")

    def test_accounting_money_filter_shares_the_setting(self):
        set_currency("AED")
        output = Template("{% load accounting_tags %}{{ value|money }}").render(Context({"value": Decimal("-1500")}))
        self.assertEqual(output, "AED -1,500.00")

    def test_primed_symbol_is_dropped_when_settings_are_saved(self):
        currency_tags.prime_currency_symbol("Rs")
        set_currency("€")
        self.assertEqual(render("{{ value|money }}", value=1), "€ 1.00")

    def test_quantity_filters(self):
        self.assertEqual(render("{{ value|qty }}", value=Decimal("2.000")), "2")
        self.assertEqual(render("{{ value|qty }}", value=Decimal("0.500")), "0.5")
        self.assertEqual(render("{{ value|qty_unit:'kg' }}", value=Decimal("1.250")), "1.25 kg")
        self.assertEqual(render("{{ value|qty_short:'pcs' }}", value=Decimal("3.000")), "3")
        self.assertEqual(render("{{ value|qty_short:'kg' }}", value=Decimal("0.250")), "0.25 kg")

    def test_per_unit_and_step_follow_the_product_unit(self):
        flour = Product(name="Flour", unit="kg")
        pen = Product(name="Pen", unit=Product.UNIT_PIECE)
        self.assertEqual(render("{{ product|per_unit }}", product=flour), " / kg")
        self.assertEqual(render("{{ product|per_unit }}", product=pen), "")
        self.assertEqual(render("{{ product|qty_step }}", product=flour), "0.001")
        self.assertEqual(render("{{ product|qty_step }}", product=pen), "1")

    def test_quantity_chips_disable_more_than_stock(self):
        chips = currency_tags.quantity_chips("kg", Decimal("0.750"))
        self.assertEqual([chip["quantity"] for chip in chips], ["0.25", "0.5", "1", "2"])
        self.assertEqual([chip["disabled"] for chip in chips], [False, False, True, True])
        self.assertEqual(currency_tags.quantity_chips(Product.UNIT_PIECE, 5), [])


class SettingsFormTests(AuthenticatedAdminTestCase):
    def _payload(self, **overrides):
        form = SystemSettingsForm(prefix="business", instance=SystemSettings.load())
        payload = {"action": "save_rules"}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if isinstance(value, bool):
                if value:
                    payload[f"business-{name}"] = "on"
            elif value is not None:
                payload[f"business-{name}"] = str(value)
        payload.update({f"business-{name}": value for name, value in overrides.items()})
        return payload

    def test_settings_page_renders_currency_and_markup_fields(self):
        response = self.client.get(reverse("settings_page"))
        self.assertEqual(response.status_code, 200)
        for name in ("currency_symbol", "currency_code", "default_markup_percent"):
            self.assertContains(response, f'name="business-{name}"')

    def test_saving_rules_updates_currency_and_default_markup(self):
        response = self.client.post(
            reverse("settings_page"),
            self._payload(currency_symbol="$", currency_code="usd", default_markup_percent="25"),
        )
        self.assertEqual(response.status_code, 302)
        settings_obj = SystemSettings.load()
        self.assertEqual(settings_obj.currency_symbol, "$")
        self.assertEqual(settings_obj.currency_code, "USD")
        self.assertEqual(settings_obj.default_markup_percent, Decimal("25.00"))

    def test_post_without_the_new_fields_keeps_stored_values(self):
        set_currency("$", "USD")
        payload = self._payload()
        for name in ("currency_symbol", "currency_code", "default_markup_percent"):
            payload.pop(f"business-{name}", None)
        self.client.post(reverse("settings_page"), payload)
        settings_obj = SystemSettings.load()
        self.assertEqual(settings_obj.currency_symbol, "$")
        self.assertEqual(settings_obj.currency_code, "USD")

    def test_bad_currency_code_and_negative_markup_are_rejected(self):
        form = SystemSettingsForm(
            self._payload(currency_code="DOLLARS", default_markup_percent="-5"),
            prefix="business",
            instance=SystemSettings.load(),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("currency_code", form.errors)
        self.assertIn("default_markup_percent", form.errors)


class CurrencyNeutralScreenTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.flour = Product.objects.create(
            name="Flour",
            sku="FLR-1",
            unit="kg",
            cost_price=Decimal("100.00"),
            price=Decimal("140.00"),
            stock=Decimal("12.500"),
        )
        self.pen = Product.objects.create(
            name="Pen",
            sku="PEN-1",
            cost_price=Decimal("1000.00"),
            price=Decimal("1500.00"),
            stock=Decimal("8"),
        )

    def _put_in_cart(self, product, quantity):
        session = self.client.session
        session["cart"] = {str(product.id): cart_line(product, quantity)}
        session.save()

    def test_pos_screen_shows_weight_price_and_stock_with_unit(self):
        self._put_in_cart(self.flour, "0.5")
        response = self.client.get(reverse("pos_screen"))
        self.assertContains(response, "Rs 140.00 / kg")
        self.assertContains(response, "12.5 kg")
        self.assertContains(response, 'name="action" value="set_quantity"')
        self.assertContains(response, 'name="quantity" value="0.5"')
        self.assertContains(response, 'step="0.001"')
        self.assertContains(response, 'data-quantity-chip="0.25"')
        self.assertContains(response, "500 g")
        self.assertNotContains(response, "12.500")

    def test_piece_products_get_whole_number_quantity_without_chips(self):
        self._put_in_cart(self.pen, "2")
        response = self.client.get(reverse("pos_screen"))
        self.assertContains(response, 'name="quantity" value="2"')
        self.assertContains(response, 'step="1"')
        self.assertNotContains(response, "data-quantity-chip=")
        self.assertContains(response, "Rs 1,500.00")

    def test_typed_weight_is_saved_to_the_cart(self):
        self._put_in_cart(self.flour, "1")
        response = self.client.post(
            reverse("pos_screen"),
            {"action": "set_quantity", "product_id": str(self.flour.id), "quantity": "0.75"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["cart"][str(self.flour.id)]["quantity"], "0.75")

    def test_pos_screen_uses_configured_currency(self):
        set_currency("$")
        self._put_in_cart(self.flour, "1")
        response = self.client.get(reverse("pos_screen"))
        self.assertContains(response, "$ 140.00 / kg")
        self.assertNotContains(response, "Rs ")

    def test_invoice_shows_quantity_with_unit_and_currency(self):
        set_currency("$")
        sale = Sale.objects.create(total_amount=Decimal("70.00"), paid_amount=Decimal("70.00"), sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.flour, quantity=Decimal("0.500"), price=Decimal("140.00"))
        response = self.client.get(reverse("invoice", args=[sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0.5 kg x $ 140.00")
        self.assertNotContains(response, "Rs ")
        self.assertNotContains(response, "POS IMS")

    def test_product_list_shows_stock_with_unit(self):
        response = self.client.get(reverse("product_list"))
        self.assertContains(response, "12.5 kg")
        self.assertContains(response, "per kg")

    def test_product_detail_template_shows_stock_and_totals_with_unit(self):
        # Rendered directly so this covers the template whatever the view aggregates.
        request = self.client.get(reverse("product_list")).wsgi_request
        html = render_to_string(
            "pos/product_view.html",
            {
                "product": self.flour,
                "total_sold": Decimal("3.250"),
                "total_purchased": Decimal("20.000"),
                "recent_sales": [],
                "recent_purchases": [],
            },
            request=request,
        )
        self.assertIn("12.5 kg", html)
        self.assertIn("3.25 kg", html)
        self.assertIn("20 kg", html)
        self.assertIn("Rs 140.00", html)
        self.assertIn("per kg", html)

    def test_product_form_offers_unit_markup_and_opening_stock(self):
        add_response = self.client.get(reverse("product_add"))
        for name in ("unit", "markup_percent", "opening_stock"):
            self.assertContains(add_response, f'name="{name}"')
        self.assertContains(add_response, "pricing-preview")

        edit_response = self.client.get(reverse("product_edit", args=[self.flour.id]))
        self.assertContains(edit_response, 'name="markup_percent"')
        self.assertNotContains(edit_response, 'name="opening_stock"')
        self.assertContains(edit_response, "12.5 kg")

    def test_purchase_screen_exposes_unit_rules_on_product_options(self):
        self.flour.markup_percent = Decimal("40.00")
        self.flour.save()
        response = self.client.get(reverse("purchase_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-unit="kg"')
        self.assertContains(response, 'data-step="0.001"')
        self.assertContains(response, 'data-markup="40.00"')
        self.assertContains(response, 'data-step="1"')
        self.assertContains(response, 'name="quick_product-unit"')
        self.assertContains(response, 'name="quick_product-markup_percent"')

    def test_stock_adjustment_screen_knows_each_products_step(self):
        response = self.client.get(reverse("stock_adjustments"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'"{self.flour.id}": {{step: "0.001", stock: "12.5 kg"}}')
        self.assertContains(response, f'"{self.pen.id}": {{step: "1", stock: "8 pcs"}}')

    def test_sale_return_inputs_accept_fractions_only_for_weighed_items(self):
        sale = Sale.objects.create(total_amount=Decimal("1570.00"), paid_amount=Decimal("1570.00"), sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.flour, quantity=Decimal("0.500"), price=Decimal("140.00"))
        SaleItem.objects.create(sale=sale, product=self.pen, quantity=Decimal("1"), price=Decimal("1500.00"))
        response = self.client.get(reverse("sale_return", args=[sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'max="0.5" step="0.001"')
        self.assertContains(response, 'max="1" step="1"')
        self.assertContains(response, "0.5 kg")


class UnitAwareFormTests(TestCase):
    def test_purchase_item_form_renders_data_attributes(self):
        Product.objects.create(name="Rope", sku="ROPE-1", unit="m", price=Decimal("30.00"), markup_percent=None)
        html = str(PurchaseItemForm(prefix="items-0")["product"])
        self.assertIn('data-unit="m"', html)
        self.assertIn('data-step="0.001"', html)
        self.assertIn('data-markup=""', html)

    def test_quick_product_accepts_markup_instead_of_price(self):
        form = QuickProductForm(
            {
                "quick_product-name": "Sugar",
                "quick_product-sku": "SUG-1",
                "quick_product-unit": "kg",
                "quick_product-markup_percent": "20",
                "quick_product-minimum_margin_percent": "5",
                "quick_product-low_stock_threshold": "2.5",
            },
            prefix="quick_product",
        )
        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()
        self.assertEqual(product.unit, "kg")
        self.assertEqual(product.markup_percent, Decimal("20"))
        self.assertEqual(product.price, Decimal("0"))

    def test_quick_product_needs_a_price_or_a_markup(self):
        form = QuickProductForm(
            {
                "quick_product-name": "Salt",
                "quick_product-sku": "SLT-1",
                "quick_product-minimum_margin_percent": "0",
                "quick_product-low_stock_threshold": "5",
            },
            prefix="quick_product",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("price", form.errors)

    def test_quick_product_defaults_to_pieces(self):
        form = QuickProductForm(
            {
                "quick_product-name": "Cup",
                "quick_product-sku": "CUP-1",
                "quick_product-price": "50",
                "quick_product-minimum_margin_percent": "0",
                "quick_product-low_stock_threshold": "5",
            },
            prefix="quick_product",
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().unit, Product.UNIT_PIECE)
