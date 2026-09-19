"""The till's JSON endpoints: same rules as the form actions, answered with the whole bill."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from pos.models import Customer, Product, Sale, SystemSettings
from pos.services.access import CASHIER_ROLE, assign_role
from pos.test_support import AuthenticatedAdminTestCase


AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _line(product, quantity="1", **overrides):
    line = {
        "name": product.name,
        "price": str(product.price),
        "original_price": str(product.price),
        "discount_percent": "0.00",
        "quantity": str(quantity),
    }
    line.update(overrides)
    return line


class TillCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.soap = Product.objects.create(
            name="Soap", sku="SOAP", barcode="111222", cost_price=Decimal("10.00"), price=Decimal("25.00"), stock=5
        )
        self.flour = Product.objects.create(
            name="Flour", sku="FLOUR", unit="kg", cost_price=Decimal("100.00"), price=Decimal("140.00"), stock=Decimal("12.500")
        )

    def till(self, action, data=None, **extra):
        return self.client.post(reverse("till_action", kwargs={"action": action}), data or {}, **AJAX, **extra)

    def set_cart(self, *lines):
        session = self.client.session
        session["cart"] = {str(product.id): _line(product, quantity) for product, quantity in lines}
        session.save()

    def cart(self):
        return self.client.session.get("cart", {})


class TillAccessTests(TillCase):
    def test_get_is_refused_and_changes_nothing(self):
        response = self.client.get(reverse("till_action", kwargs={"action": "add"}), {"product_id": self.soap.id})

        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.cart(), {})

    def test_post_needs_a_csrf_token(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin_user)

        response = strict.post(reverse("till_action", kwargs={"action": "add"}), {"product_id": self.soap.id})

        self.assertEqual(response.status_code, 403)

    def test_anonymous_visitor_gets_json_401_and_a_redirect_without_ajax(self):
        anonymous = Client()
        url = reverse("till_action", kwargs={"action": "add"})

        self.assertEqual(anonymous.post(url, {"product_id": self.soap.id}, **AJAX).status_code, 401)
        plain = anonymous.post(url, {"product_id": self.soap.id})
        self.assertEqual(plain.status_code, 302)
        self.assertIn(reverse("login"), plain.url)
        self.assertEqual(anonymous.get(reverse("till_catalogue")).status_code, 302)

    def test_staff_without_a_till_role_are_turned_away(self):
        outsider = get_user_model().objects.create_user(username="outsider", password="OutsidePass123!")
        client = Client()
        client.force_login(outsider)

        response = client.post(reverse("till_action", kwargs={"action": "add"}), {"product_id": self.soap.id})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.session.get("cart", {}), {})

    def test_cashier_may_ring_up(self):
        cashier = get_user_model().objects.create_user(username="till", password="TillPass123!x")
        assign_role(cashier, CASHIER_ROLE)
        client = Client()
        client.force_login(cashier)

        response = client.post(reverse("till_action", kwargs={"action": "add"}), {"product_id": self.soap.id}, **AJAX)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.session["cart"][str(self.soap.id)]["quantity"], "1")

    def test_unknown_action_is_a_404(self):
        self.assertEqual(self.till("teleport").status_code, 404)


class TillAddAndScanTests(TillCase):
    def test_add_answers_with_the_whole_bill(self):
        data = self.till("add", {"product_id": self.soap.id}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["selected"], str(self.soap.id))
        self.assertEqual(data["lines"][0]["quantity"], "1")
        self.assertEqual(data["lines"][0]["amount"], "25.00")
        self.assertEqual(data["totals"]["total"], "25.00")
        self.assertIn('data-line="%s"' % self.soap.id, data["html"]["bill"])
        self.assertIn('name="quantity" value="1"', data["html"]["bill"])
        self.assertFalse(data["checkout_disabled"])

    def test_adding_again_is_one_more_like_the_form_action(self):
        self.till("add", {"product_id": self.soap.id})
        self.till("add", {"product_id": self.soap.id})
        json_quantity = self.cart()[str(self.soap.id)]["quantity"]

        self.set_cart()
        self.client.post(reverse("pos_screen"), {"action": "add", "product_id": self.soap.id})
        self.client.post(reverse("pos_screen"), {"action": "add", "product_id": self.soap.id})

        self.assertEqual(json_quantity, "2")
        self.assertEqual(self.cart()[str(self.soap.id)]["quantity"], json_quantity)

    def test_add_stops_at_the_shelf_with_the_form_actions_words(self):
        self.set_cart((self.soap, 5))

        data = self.till("add", {"product_id": self.soap.id}).json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "out_of_stock")
        self.assertEqual(data["notice"]["text"], "Soap has no more stock available.")
        self.assertEqual(self.cart()[str(self.soap.id)]["quantity"], "5")

    def test_inactive_or_missing_product_is_a_404_with_the_bill(self):
        self.soap.is_active = False
        self.soap.save()

        response = self.till("add", {"product_id": self.soap.id})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], "notfound")
        self.assertEqual(self.till("add", {"product_id": "abc"}).status_code, 404)
        self.assertEqual(self.cart(), {})

    def test_scan_finds_barcode_or_sku_in_any_case(self):
        by_barcode = self.till("scan", {"code": "111222"}).json()
        by_sku = self.till("scan", {"code": "soap"}).json()

        self.assertTrue(by_barcode["ok"])
        self.assertTrue(by_sku["ok"])
        self.assertEqual(self.cart()[str(self.soap.id)]["quantity"], "2")

    def test_unknown_code_offers_create_product_only_to_those_who_may(self):
        data = self.till("scan", {"code": "NOPE-1"}).json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "notfound")
        self.assertEqual(data["notice"]["code"], "NOPE-1")
        self.assertTrue(data["notice"]["can_create"])

        cashier = get_user_model().objects.create_user(username="till", password="TillPass123!x")
        assign_role(cashier, CASHIER_ROLE)
        client = Client()
        client.force_login(cashier)
        refused = client.post(reverse("till_action", kwargs={"action": "scan"}), {"code": "NOPE-1"}, **AJAX).json()
        self.assertFalse(refused["notice"]["can_create"])

    def test_the_old_scan_endpoint_keeps_its_contract(self):
        response = self.client.post(reverse("scan_barcode", args=["111222"]))

        self.assertEqual(response.json(), {"status": "ok", "product_id": self.soap.id})


class TillFractionTests(TillCase):
    def test_typed_weight_leads_the_line(self):
        data = self.till("add", {"product_id": self.flour.id, "quantity": "0.5"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["lines"][0]["quantity"], "0.5")
        self.assertEqual(data["lines"][0]["unit"], "kg")
        self.assertEqual(data["lines"][0]["amount"], "70.00")
        self.assertIn('data-quantity-chip="0.25"', data["html"]["bill"])
        self.assertNotIn("12.500", data["html"]["bill"])

    def test_grams_become_kilograms(self):
        data = self.till("scan", {"code": "FLOUR", "quantity": "500", "quantity_unit": "g"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(self.cart()[str(self.flour.id)]["quantity"], "0.5")

    def test_a_typed_quantity_adds_to_the_line(self):
        self.set_cart((self.flour, "1"))

        self.till("add", {"product_id": self.flour.id, "quantity": "250", "quantity_unit": "g"})

        self.assertEqual(self.cart()[str(self.flour.id)]["quantity"], "1.25")

    def test_a_unit_the_product_is_not_sold_by_is_refused(self):
        data = self.till("add", {"product_id": self.soap.id, "quantity": "500", "quantity_unit": "g"}).json()

        self.assertFalse(data["ok"])
        self.assertIn("not by g", data["notice"]["text"])
        self.assertEqual(self.cart(), {})

    def test_piece_goods_refuse_fractions_with_the_form_actions_words(self):
        json_notice = self.till("add", {"product_id": self.soap.id, "quantity": "0.5"}).json()["notice"]["text"]
        self.assertEqual(self.cart(), {})

        form = self.client.post(
            reverse("pos_screen"), {"action": "set_quantity", "product_id": self.soap.id, "quantity": "0.5"}, follow=True
        )
        form_messages = [str(message) for message in form.context["messages"]]

        self.assertEqual([json_notice], form_messages)

    def test_more_than_the_shelf_holds_is_refused_and_the_line_keeps_its_quantity(self):
        self.set_cart((self.flour, "1"))

        data = self.till("quantity", {"product_id": self.flour.id, "quantity": "20"}).json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["notice"]["text"], "Flour: Only 12.5 kg in stock.")
        self.assertEqual(self.cart()[str(self.flour.id)]["quantity"], "1")

    def test_set_quantity_matches_the_form_action(self):
        self.set_cart((self.flour, "1"))
        self.till("quantity", {"product_id": self.flour.id, "quantity": "0.75"})
        json_quantity = self.cart()[str(self.flour.id)]["quantity"]

        self.set_cart((self.flour, "1"))
        self.client.post(reverse("pos_screen"), {"action": "set_quantity", "product_id": self.flour.id, "quantity": "0.75"})

        self.assertEqual(json_quantity, "0.75")
        self.assertEqual(self.cart()[str(self.flour.id)]["quantity"], json_quantity)

    def test_zero_quantity_removes_the_line(self):
        self.set_cart((self.flour, "1"))

        data = self.till("quantity", {"product_id": self.flour.id, "quantity": "0"}).json()

        self.assertEqual(data["lines"], [])
        self.assertEqual(self.cart(), {})


class TillLineTests(TillCase):
    def test_increase_decrease_remove_and_clear(self):
        self.set_cart((self.soap, 2), (self.flour, "1"))

        self.till("increase", {"product_id": self.soap.id})
        self.assertEqual(self.cart()[str(self.soap.id)]["quantity"], "3")
        self.till("decrease", {"product_id": self.soap.id})
        self.till("decrease", {"product_id": self.soap.id})
        self.assertEqual(self.cart()[str(self.soap.id)]["quantity"], "1")
        self.till("decrease", {"product_id": self.soap.id})
        self.assertNotIn(str(self.soap.id), self.cart())

        removed = self.till("remove", {"product_id": self.flour.id}).json()
        self.assertEqual(removed["lines"], [])

        self.set_cart((self.soap, 2))
        cleared = self.till("clear").json()
        self.assertTrue(cleared["ok"])
        self.assertEqual(self.cart(), {})

    def test_price_below_the_minimum_margin_is_refused_like_the_form_action(self):
        settings_row = SystemSettings.load()
        settings_row.allow_price_override = True
        settings_row.save()
        self.set_cart((self.soap, 1))

        data = self.till("price", {"product_id": self.soap.id, "custom_price": "1.00"}).json()
        form = self.client.post(
            reverse("pos_screen"),
            {"action": "update_line", "product_id": self.soap.id, "custom_price": "1.00", "line_discount": "0"},
            follow=True,
        )
        form_messages = [str(message) for message in form.context["messages"]]

        self.assertFalse(data["ok"])
        self.assertIn("cannot go below", data["notice"]["text"])
        self.assertEqual([data["notice"]["text"]], form_messages)
        self.assertEqual(self.cart()[str(self.soap.id)]["price"], "25.00")

    def test_a_fair_price_and_discount_are_saved(self):
        settings_row = SystemSettings.load()
        settings_row.allow_price_override = True
        settings_row.allow_line_discount = True
        settings_row.save()
        self.set_cart((self.soap, 2))

        self.till("price", {"product_id": self.soap.id, "custom_price": "30.00"})
        data = self.till("price", {"product_id": self.soap.id, "line_discount": "10"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["lines"][0]["rate"], "30.00")
        self.assertEqual(data["lines"][0]["discount_percent"], "10.00")
        self.assertEqual(data["lines"][0]["amount"], "54.00")
        self.assertEqual(data["totals"]["line_discount"], "6.00")

    def test_settings_that_forbid_override_and_discount_hold_on_this_path_too(self):
        settings_row = SystemSettings.load()
        settings_row.allow_price_override = False
        settings_row.allow_line_discount = False
        settings_row.save()
        self.set_cart((self.soap, 1))

        data = self.till("price", {"product_id": self.soap.id, "custom_price": "40.00", "line_discount": "5"}).json()

        self.assertFalse(data["ok"])
        self.assertIn("Price override is disabled in Settings.", data["notice"]["text"])
        self.assertIn("Line discount is disabled in Settings.", data["notice"]["text"])
        line = self.cart()[str(self.soap.id)]
        self.assertEqual(line["price"], "25.00")
        self.assertEqual(line["discount_percent"], "0.00")
        self.assertIn("readonly", data["html"]["bill"])

    def test_a_line_short_of_stock_is_marked_and_blocks_the_sale(self):
        self.set_cart((self.soap, 9))

        data = self.till("context").json()

        self.assertTrue(data["checkout_disabled"])
        self.assertEqual(data["lines"][0]["stock_error"], "Only 5 pcs in stock.")
        self.assertIn("t-line-note", data["html"]["bill"])
        self.assertIn("has-issue", data["html"]["bill"])


class TillContextTests(TillCase):
    def test_bill_discount_moves_the_total(self):
        self.set_cart((self.soap, 4))

        data = self.till("context", {"discount_rate": "10"}).json()

        self.assertEqual(data["totals"]["subtotal"], "100.00")
        self.assertEqual(data["totals"]["discount"], "10.00")
        self.assertEqual(data["totals"]["total"], "90.00")
        self.assertEqual(Decimal(data["context"]["discount_rate"]), Decimal("10"))

    def test_context_is_remembered_while_the_bill_has_lines(self):
        customer = Customer.objects.create(name="Ayesha")
        self.set_cart((self.soap, 1))
        self.till("context", {"customer_id": customer.id, "credit": "1", "payment_method": "bank", "discount_rate": "5"})

        page = self.client.get(reverse("pos_screen"))

        self.assertEqual(page.context["selected_customer_id"], str(customer.id))
        self.assertEqual(page.context["payment_method_value"], "bank")
        self.assertEqual(page.context["discount_rate_value"], Decimal("5.00"))
        self.assertEqual(page.context["prefill_credit_sale"], bool(SystemSettings.load().allow_credit_sales))

    def test_an_address_with_its_own_values_wins_over_the_remembered_context(self):
        self.set_cart((self.soap, 1))
        self.till("context", {"payment_method": "bank", "discount_rate": "5"})

        page = self.client.get(reverse("pos_screen"), {"payment_method": "cash"})

        self.assertEqual(page.context["payment_method_value"], "cash")
        self.assertEqual(page.context["discount_rate_value"], Decimal("0"))

    def test_an_empty_bill_forgets_the_context(self):
        customer = Customer.objects.create(name="Ayesha")
        self.set_cart((self.soap, 1))
        self.till("context", {"customer_id": customer.id})

        self.till("remove", {"product_id": self.soap.id})
        page = self.client.get(reverse("pos_screen"))

        self.assertNotIn("till_context", self.client.session)
        self.assertEqual(page.context["selected_customer_id"], "")

    def test_unknown_customer_bad_method_and_credit_when_disabled_are_cleaned(self):
        settings_row = SystemSettings.load()
        settings_row.allow_credit_sales = False
        settings_row.save()
        self.set_cart((self.soap, 1))

        context = self.till(
            "context", {"customer_id": "99999", "credit": "1", "payment_method": "cheque", "discount_rate": "250"}
        ).json()["context"]

        self.assertEqual(context["customer_id"], "")
        self.assertFalse(context["credit"])
        self.assertEqual(context["payment_method"], Sale.CASH)
        self.assertEqual(Decimal(context["discount_rate"]), Decimal("100"))

    def test_the_selected_customer_comes_back_with_their_balance(self):
        customer = Customer.objects.create(name="Ayesha")
        self.set_cart((self.soap, 1))

        data = self.till("context", {"customer_id": customer.id}).json()

        self.assertEqual(data["customer"]["name"], "Ayesha")
        self.assertEqual(data["customer"]["balance_type"], "clear")


class TillHeldBillTests(TillCase):
    def test_hold_parks_the_bill_with_its_context_and_resume_brings_both_back(self):
        customer = Customer.objects.create(name="Ayesha")
        self.set_cart((self.flour, "0.5"))

        held = self.till("hold", {"customer_id": customer.id, "discount_rate": "5"}).json()

        self.assertTrue(held["ok"])
        self.assertEqual(held["lines"], [])
        self.assertEqual(held["context"]["customer_id"], "")
        self.assertEqual(held["held"][0]["label"], "Ayesha")
        self.assertEqual(held["held"][0]["total"], "70.00")
        self.assertIn("Ayesha", held["html"]["held"])
        self.assertEqual(self.cart(), {})

        resumed = self.till("resume", {"held_id": held["held"][0]["id"]}).json()

        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["lines"][0]["quantity"], "0.5")
        self.assertEqual(resumed["context"]["customer_id"], str(customer.id))
        self.assertEqual(Decimal(resumed["context"]["discount_rate"]), Decimal("5"))
        self.assertEqual(resumed["held"], [])

    def test_resuming_with_a_bill_on_screen_swaps_instead_of_losing_it(self):
        self.set_cart((self.soap, 1))
        held_id = self.till("hold", {"hold_label": "Table 4"}).json()["held"][0]["id"]
        self.set_cart((self.flour, "2"))

        data = self.till("resume", {"held_id": held_id}).json()

        self.assertEqual([line["product_id"] for line in data["lines"]], [str(self.soap.id)])
        self.assertEqual(len(data["held"]), 1)
        self.assertEqual(list(self.client.session["held_carts"][0]["cart"]), [str(self.flour.id)])

    def test_empty_bill_cannot_be_held_with_the_form_actions_words(self):
        data = self.till("hold").json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["notice"]["text"], "Add at least one item before holding the sale.")
        self.assertFalse(self.client.session.get("held_carts"))

    def test_discard_and_stale_ids(self):
        self.set_cart((self.soap, 1))
        held_id = self.till("hold", {"hold_label": "Walk-in"}).json()["held"][0]["id"]

        discarded = self.till("discard", {"held_id": held_id}).json()
        stale = self.till("resume", {"held_id": held_id}).json()

        self.assertTrue(discarded["ok"])
        self.assertEqual(discarded["held"], [])
        self.assertFalse(stale["ok"])
        self.assertIn("no longer available", stale["notice"]["text"])

    def test_hold_endpoints_are_closed_with_the_module(self):
        self.set_cart((self.soap, 1))

        with mock.patch("pos.views.pos_views.module_enabled", return_value=False):
            responses = [self.till(action, {"held_id": "x"}) for action in ("hold", "resume", "discard")]

        self.assertEqual([response.status_code for response in responses], [403, 403, 403])
        self.assertEqual(self.cart()[str(self.soap.id)]["quantity"], "1")

    def test_a_bill_held_by_the_form_action_resumes_through_the_till(self):
        self.set_cart((self.soap, 2))
        self.client.post(reverse("pos_screen"), {"action": "hold_cart", "hold_label": "Counter 2"})
        held_id = self.client.session["held_carts"][0]["id"]

        data = self.till("resume", {"held_id": held_id}).json()

        self.assertEqual(data["lines"][0]["quantity"], "2")


class TillCheckoutTests(TillCase):
    def checkout(self, **data):
        payload = {"action": "checkout", "respond": "json", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"}
        payload.update(data)
        return self.client.post(reverse("pos_screen"), payload, **AJAX)

    def test_a_closed_sale_answers_with_the_receipt_and_the_stamp(self):
        self.set_cart((self.soap, 2))
        self.till("context", {"discount_rate": "0"})

        data = self.checkout().json()

        sale = Sale.objects.get()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stamp"], "Paid")
        self.assertEqual(data["redirect"], reverse("invoice", args=[sale.id]))
        self.assertEqual(sale.total_amount, Decimal("50.00"))
        self.assertEqual(self.cart(), {})
        self.assertNotIn("till_context", self.client.session)
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.stock, 3)

    def test_a_credit_sale_is_stamped_credit(self):
        settings_row = SystemSettings.load()
        settings_row.allow_credit_sales = True
        settings_row.save()
        customer = Customer.objects.create(name="Ayesha")
        self.set_cart((self.soap, 1))

        data = self.checkout(customer_id=customer.id, selected_customer_id=customer.id, is_credit="on").json()

        self.assertEqual(data["stamp"], "Credit")
        self.assertTrue(Sale.objects.get().is_credit)

    def test_a_refused_sale_answers_with_the_reason_and_the_bill(self):
        settings_row = SystemSettings.load()
        settings_row.allow_credit_sales = True
        settings_row.save()
        self.set_cart((self.soap, 1))

        data = self.checkout(is_credit="on").json()

        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "refused")
        self.assertEqual(data["notice"]["text"], "Credit sale requires an active customer.")
        self.assertEqual(len(data["lines"]), 1)
        self.assertEqual(Sale.objects.count(), 0)

    def test_without_the_json_flag_checkout_still_redirects_to_the_receipt(self):
        self.set_cart((self.soap, 1))

        response = self.client.post(
            reverse("pos_screen"), {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"}
        )

        self.assertRedirects(response, reverse("invoice", args=[Sale.objects.get().id]), fetch_redirect_response=False)


class TillCatalogueAndCustomerTests(TillCase):
    def test_catalogue_lists_active_products_with_display_figures(self):
        Product.objects.create(name="Gone", sku="GONE", cost_price=1, price=2, stock=1, is_active=False)
        service = Product.objects.create(name="Sharpening", sku="SRV", cost_price=0, price=100, stock=0, track_stock=False)

        products = {row["sku"]: row for row in self.client.get(reverse("till_catalogue")).json()["products"]}

        self.assertNotIn("GONE", products)
        self.assertEqual(products["FLOUR"]["stock"], "12.5 kg")
        self.assertTrue(products["FLOUR"]["fraction"])
        self.assertTrue(products["FLOUR"]["price"].endswith("140.00 / kg"))
        self.assertIn("111222", products["SOAP"]["find"])
        self.assertEqual(products["SRV"]["stock"], "Not tracked")
        self.assertFalse(products["SRV"]["out"])
        self.assertEqual(service.stock, 0)

    def test_new_customer_from_the_side_panel(self):
        response = self.client.post(reverse("till_customer_add"), {"name": "Haji Rafiq", "phone": "0300"}, **AJAX)

        self.assertEqual(response.status_code, 200)
        customer = Customer.objects.get(name="Haji Rafiq")
        self.assertEqual(response.json(), {"ok": True, "customer": {"id": customer.id, "name": "Haji Rafiq"}})

    def test_new_customer_needs_a_name(self):
        response = self.client.post(reverse("till_customer_add"), {"name": ""}, **AJAX)

        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json()["errors"])
        self.assertEqual(Customer.objects.count(), 0)


class TillPageTests(TillCase):
    def test_the_page_keeps_the_form_fallback_and_the_json_transport(self):
        self.set_cart((self.flour, "0.5"))

        page = self.client.get(reverse("pos_screen"))

        self.assertContains(page, 'id="barcode-input"')
        self.assertContains(page, 'name="action" value="set_quantity"')
        self.assertContains(page, 'name="action" value="update_line"')
        self.assertContains(page, 'name="action" value="remove_all"')
        self.assertContains(page, 'name="action" value="checkout"')
        self.assertContains(page, 'name="action" value="hold_cart"')
        self.assertContains(page, "zehnms/js/till.js")
        self.assertContains(page, reverse("till_action", kwargs={"action": "__action__"}))
        self.assertNotContains(page, "<style>.product-browser-card")

    def test_create_product_link_is_only_handed_to_those_who_may_manage_products(self):
        page = self.client.get(reverse("pos_screen"))
        self.assertContains(page, "data-product-add-url")

        cashier = get_user_model().objects.create_user(username="till", password="TillPass123!x")
        assign_role(cashier, CASHIER_ROLE)
        client = Client()
        client.force_login(cashier)
        self.assertNotContains(client.get(reverse("pos_screen")), "data-product-add-url")
