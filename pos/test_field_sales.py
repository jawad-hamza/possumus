"""Customers of a distribution business: city, route, order booker, credit limit, the customer
ledger laid out as a statement, and the receivables / order booker reports."""
import csv
import datetime
import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog
from accounts.services.chart_of_accounts import ensure_default_accounts
from accounts.services.current_actor import acting_as
from accounts.services.party_balance import get_party_balance
from accounts.services.party_opening import THEY_OWE_US, set_party_opening_balance
from accounts.services.party_statement import get_party_statement
from accounts.services.settlements import create_receipt
from pos.forms import CustomerForm, SupplierForm, SystemSettingsForm
from pos.models import Customer, OrderBooker, Product, Route, Sale, SaleItem, Supplier, SystemSettings
from pos.services import field_sales, modules
from pos.services.access import CASHIER_ROLE, assign_role
from pos.services.field_sales_reports import build_receivables_by_area, build_sales_by_order_booker
from pos.services.invoice_quantity import invoice_quantity
from pos.services.reporting import report_navigation
from pos.services.returns import create_sale_return
from pos.test_support import AuthenticatedAdminTestCase

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def read_csv(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


class FieldSalesCase(AuthenticatedAdminTestCase):
    """A small distributor: two routes, two order bookers, flour by the kilo and soap by the piece."""

    def setUp(self):
        super().setUp()
        ensure_default_accounts()
        self.today = timezone.localdate()
        self.flour = Product.objects.create(
            name="Flour", sku="FLOUR", unit="kg", cost_price=Decimal("100.00"), price=Decimal("150.00"), stock=Decimal("500.000")
        )
        self.soap = Product.objects.create(
            name="Soap", sku="SOAP", cost_price=Decimal("20.00"), price=Decimal("50.00"), stock=Decimal("200.000")
        )
        self.bazaar = Route.objects.create(name="Main Bazaar", city="Mirpur A.K")
        self.canal = Route.objects.create(name="Canal Road", city="Dadyal")
        self.imran = OrderBooker.objects.create(name="Imran", phone="0300-1111111")
        self.bilal = OrderBooker.objects.create(name="Bilal")
        self.imran.routes.add(self.bazaar)

    # -- helpers ---------------------------------------------------------------------

    def customer(self, name="Ali Traders", **fields):
        return Customer.objects.create(name=name, **fields)

    def set_cart(self, *lines):
        session = self.client.session
        session["cart"] = {
            str(product.id): {
                "name": product.name,
                "price": str(product.price),
                "original_price": str(product.price),
                "discount_percent": "0.00",
                "quantity": str(quantity),
            }
            for product, quantity in lines
        }
        session.save()

    def checkout(self, customer=None, *, credit=False, json=False, **extra):
        data = {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"}
        if customer is not None:
            data["selected_customer_id"] = str(customer.id)
        if credit:
            data["is_credit"] = "on"
        data.update(extra)
        if json:
            data["respond"] = "json"
            return self.client.post(reverse("pos_screen"), data, **AJAX)
        return self.client.post(reverse("pos_screen"), data)

    def credit_sale(self, customer, *lines, **extra):
        """Sell on credit through the till and hand back the sale."""
        self.set_cart(*lines)
        response = self.checkout(customer, credit=True, **extra)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and [str(m) for m in response.context["messages"]])
        return Sale.objects.order_by("-id").first()

    def messages(self, response):
        return [str(message) for message in response.context["messages"]]


# =====================================================================================
# Master data: routes and order bookers
# =====================================================================================

class RouteAndOrderBookerScreenTests(FieldSalesCase):
    def test_lists_show_who_covers_what_and_how_many_customers(self):
        self.customer(route=self.bazaar, order_booker=self.imran)

        routes = self.client.get(reverse("route_list"))
        bookers = self.client.get(reverse("order_booker_list"))

        self.assertEqual(routes.status_code, 200)
        self.assertContains(routes, "Main Bazaar")
        self.assertContains(routes, "Mirpur A.K")
        row = next(route for route in routes.context["routes"] if route.pk == self.bazaar.pk)
        self.assertEqual((row.booker_names, row.customer_count), ("Imran", 1))
        self.assertEqual(bookers.status_code, 200)
        booker = next(item for item in bookers.context["bookers"] if item.pk == self.imran.pk)
        self.assertEqual((booker.route_names, booker.customer_count), ("Main Bazaar (Mirpur A.K)", 1))

    def test_route_is_added_and_edited_and_keeps_one_spelling_of_the_city(self):
        self.customer(city="Mirpur A.K")

        response = self.client.post(reverse("route_add"), {"name": "  Nangi  Road ", "city": "mirpur a.k", "is_active": "on"})

        self.assertRedirects(response, reverse("route_list") + f"#touched-{Route.objects.get(name='Nangi Road').pk}", fetch_redirect_response=False)
        route = Route.objects.get(name="Nangi Road")
        self.assertEqual(route.city, "Mirpur A.K")

        self.client.post(reverse("route_edit", args=[route.pk]), {"name": "Nangi Road", "city": "Mirpur A.K"})
        route.refresh_from_db()
        self.assertFalse(route.is_active)

    def test_same_route_name_twice_in_one_city_is_refused(self):
        response = self.client.post(reverse("route_add"), {"name": "main bazaar", "city": "Mirpur A.K", "is_active": "on"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("already a route called", str(response.context["form"].errors))
        # The same name in another city is a different round.
        other = self.client.post(reverse("route_add"), {"name": "Main Bazaar", "city": "Kotli", "is_active": "on"})
        self.assertEqual(other.status_code, 302)
        self.assertEqual(Route.objects.filter(name__iexact="main bazaar").count(), 2)

    def test_order_booker_is_added_with_routes_and_can_be_retired(self):
        response = self.client.post(
            reverse("order_booker_add"),
            {"name": "Kashif", "phone": "0333", "is_active": "on", "routes": [self.bazaar.pk, self.canal.pk]},
        )

        self.assertEqual(response.status_code, 302)
        kashif = OrderBooker.objects.get(name="Kashif")
        self.assertEqual(set(kashif.routes.all()), {self.bazaar, self.canal})

        self.client.post(reverse("order_booker_edit", args=[kashif.pk]), {"name": "Kashif", "phone": "0333", "routes": [self.canal.pk]})
        kashif.refresh_from_db()
        self.assertFalse(kashif.is_active)
        self.assertEqual(list(kashif.routes.all()), [self.canal])

    def test_two_order_bookers_cannot_share_a_name(self):
        response = self.client.post(reverse("order_booker_add"), {"name": "imran", "is_active": "on"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("already an order booker called", str(response.context["form"].errors))

    def test_side_panel_endpoints_answer_with_the_new_record(self):
        route = self.client.post(reverse("quick_create_route"), {"quick_route-name": "Kotli Road", "quick_route-city": "Kotli"}, **AJAX)
        booker = self.client.post(reverse("quick_create_order_booker"), {"quick_order_booker-name": "Saad"}, **AJAX)
        bad = self.client.post(reverse("quick_create_route"), {"quick_route-name": ""}, **AJAX)

        self.assertEqual(route.status_code, 200)
        self.assertEqual(route.json()["route"]["name"], "Kotli Road (Kotli)")
        self.assertTrue(Route.objects.filter(name="Kotli Road", city="Kotli").exists())
        self.assertEqual(booker.json()["order_booker"]["name"], "Saad")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("name", bad.json()["errors"])

    def test_order_booker_form_offers_add_route_in_place(self):
        response = self.client.get(reverse("order_booker_add"))

        self.assertContains(response, 'data-rf-quick="route"')
        self.assertContains(response, reverse("quick_create_route"))

    def test_a_cashier_cannot_open_or_change_the_lists(self):
        cashier = get_user_model().objects.create_user(username="till", password="CashierPass123!")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)

        self.assertEqual(self.client.get(reverse("route_list")).status_code, 302)
        self.assertEqual(self.client.post(reverse("route_add"), {"name": "Sneaky"}).status_code, 302)
        self.assertFalse(Route.objects.filter(name="Sneaky").exists())
        response = self.client.post(reverse("quick_create_order_booker"), {"quick_order_booker-name": "Sneaky"}, **AJAX)
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(OrderBooker.objects.filter(name="Sneaky").exists())


# =====================================================================================
# Customers: city, route, order booker, credit limit
# =====================================================================================

class CustomerFormTests(FieldSalesCase):
    def form(self, data, instance=None):
        with acting_as(self.admin_user):
            form = CustomerForm(data, instance=instance)
            self.assertTrue(form.is_valid(), form.errors)
            return form.save()

    def test_route_brings_its_only_order_booker_and_its_city(self):
        customer = self.form({"name": "Ali Traders", "route": self.bazaar.pk})

        self.assertEqual(customer.route, self.bazaar)
        self.assertEqual(customer.order_booker, self.imran)
        self.assertEqual(customer.city, "Mirpur A.K")

    def test_no_order_booker_is_guessed_when_a_route_has_none_or_several(self):
        self.bilal.routes.add(self.bazaar)

        shared = self.form({"name": "Shared", "route": self.bazaar.pk})
        nobody = self.form({"name": "Nobody", "route": self.canal.pk})

        self.assertIsNone(shared.order_booker)
        self.assertIsNone(nobody.order_booker)

    def test_a_chosen_order_booker_and_city_are_kept(self):
        customer = self.form({"name": "Ali", "route": self.bazaar.pk, "order_booker": self.bilal.pk, "city": "Kotli"})

        self.assertEqual(customer.order_booker, self.bilal)
        self.assertEqual(customer.city, "Kotli")

    def test_clearing_the_order_booker_on_an_edit_is_respected(self):
        customer = self.form({"name": "Ali", "route": self.bazaar.pk})
        self.assertEqual(customer.order_booker, self.imran)

        customer = self.form({"name": "Ali", "route": self.bazaar.pk, "city": "Mirpur A.K"}, instance=customer)

        self.assertIsNone(customer.order_booker)

    def test_city_takes_the_spelling_already_on_record(self):
        self.customer("First", city="Mirpur A.K")

        customer = self.form({"name": "Second", "city": "  mirpur   a.k "})

        self.assertEqual(customer.city, "Mirpur A.K")
        self.assertEqual(field_sales.known_cities(), ["Dadyal", "Mirpur A.K"])

    def test_credit_limit_is_optional_and_cannot_be_negative(self):
        self.assertIsNone(self.form({"name": "No limit"}).credit_limit)
        self.assertEqual(self.form({"name": "Limited", "credit_limit": "50000"}).credit_limit, Decimal("50000.00"))
        with acting_as(self.admin_user):
            form = CustomerForm({"name": "Wrong", "credit_limit": "-1"})
        self.assertFalse(form.is_valid())
        self.assertIn("credit_limit", form.errors)

    def test_retired_route_and_order_booker_are_not_offered_but_stay_on_their_customers(self):
        customer = self.customer(route=self.bazaar, order_booker=self.imran)
        self.bazaar.is_active = False
        self.bazaar.save()
        self.imran.is_active = False
        self.imran.save()

        with acting_as(self.admin_user):
            fresh = CustomerForm()
            editing = CustomerForm(instance=customer)

        self.assertNotIn(self.bazaar, fresh.fields["route"].queryset)
        self.assertNotIn(self.imran, fresh.fields["order_booker"].queryset)
        self.assertIn(self.bazaar, editing.fields["route"].queryset)
        self.assertIn(self.imran, editing.fields["order_booker"].queryset)

    def test_with_the_module_off_only_city_and_credit_limit_remain(self):
        modules.set_module_switch("field_sales", False)

        with acting_as(self.admin_user):
            form = CustomerForm({"name": "Retail", "city": "Kotli", "credit_limit": "100", "route": self.bazaar.pk})
            self.assertTrue(form.is_valid(), form.errors)
            customer = form.save()

        self.assertEqual(set(form.fields), {"name", "phone", "email", "address", "city", "credit_limit"})
        self.assertIsNone(customer.route)
        self.assertEqual((customer.city, customer.credit_limit), ("Kotli", Decimal("100.00")))

    def test_supplier_has_a_city_with_the_same_spelling_rule(self):
        self.customer("First", city="Mirpur A.K")

        form = SupplierForm({"name": "Flour Mills", "city": "MIRPUR A.K"})

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().city, "Mirpur A.K")


class CustomerScreenTests(FieldSalesCase):
    def test_add_page_has_the_new_fields_and_add_in_place_panels(self):
        response = self.client.get(reverse("customer_add"))

        self.assertEqual(response.status_code, 200)
        for name in ("city", "route", "order_booker", "credit_limit"):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'data-rf-quick="route"')
        self.assertContains(response, 'data-rf-quick="order_booker"')
        self.assertContains(response, 'id="fs-route-defaults"')
        self.assertContains(response, '<datalist id="party-city-options">')

    def test_customer_is_added_through_the_page(self):
        response = self.client.post(
            reverse("customer_add"),
            {"name": "Ali Traders", "phone": "0300", "city": "Mirpur A.K", "route": self.bazaar.pk, "credit_limit": "25000"},
        )

        customer = Customer.objects.get(name="Ali Traders")
        self.assertRedirects(response, reverse("customer_detail", args=[customer.pk]), fetch_redirect_response=False)
        self.assertEqual((customer.route, customer.order_booker, customer.credit_limit), (self.bazaar, self.imran, Decimal("25000.00")))

    def test_edit_page_changes_the_customer_and_logs_a_new_credit_limit(self):
        customer = self.customer(city="Kotli", credit_limit=Decimal("1000.00"))

        page = self.client.get(reverse("customer_edit", args=[customer.pk]))
        response = self.client.post(
            reverse("customer_edit", args=[customer.pk]),
            {"name": "Ali & Sons", "city": "Kotli", "route": self.canal.pk, "order_booker": self.bilal.pk, "credit_limit": "5000"},
        )

        self.assertContains(page, "Edit customer")
        self.assertRedirects(response, reverse("customer_detail", args=[customer.pk]), fetch_redirect_response=False)
        customer.refresh_from_db()
        self.assertEqual((customer.name, customer.route, customer.order_booker), ("Ali & Sons", self.canal, self.bilal))
        self.assertEqual(customer.credit_limit, Decimal("5000.00"))
        log = AuditLog.objects.get(action="customer_credit_limit_changed")
        self.assertEqual((log.metadata["before"], log.metadata["after"]), ("1000.00", "5000.00"))

    def test_customer_page_shows_route_order_booker_and_what_is_left_of_the_limit(self):
        customer = self.customer(city="Mirpur A.K", route=self.bazaar, order_booker=self.imran, credit_limit=Decimal("1000.00"))
        self.credit_sale(customer, (self.soap, 4))  # 200.00 on credit

        response = self.client.get(reverse("customer_detail", args=[customer.pk]))

        self.assertContains(response, "Mirpur A.K")
        self.assertContains(response, "Main Bazaar")
        self.assertContains(response, "Imran")
        self.assertContains(response, "Rs 1,000.00")
        self.assertContains(response, "Rs 800.00</strong> left")
        self.assertContains(response, reverse("customer_edit", args=[customer.pk]))
        self.assertContains(response, "statement: true")

    def test_list_shows_and_filters_by_city_route_and_order_booker(self):
        ali = self.customer("Ali", city="Mirpur A.K", route=self.bazaar, order_booker=self.imran)
        babar = self.customer("Babar", city="Dadyal", route=self.canal, order_booker=self.bilal)
        chand = self.customer("Chand")

        def names(**params):
            return [row.name for row in self.client.get(reverse("customer_list"), params).context["customers"]]

        page = self.client.get(reverse("customer_list"))
        self.assertContains(page, "Main Bazaar")
        self.assertContains(page, "Bilal")
        self.assertEqual(names(), ["Ali", "Babar", "Chand"])
        self.assertEqual(names(city="mirpur a.k"), ["Ali"])
        self.assertEqual(names(route=self.canal.pk), ["Babar"])
        self.assertEqual(names(order_booker=self.imran.pk), ["Ali"])
        self.assertEqual(names(city="none"), ["Chand"])
        self.assertEqual(names(order_booker="none"), ["Chand"])
        self.assertEqual(names(search="dadyal"), ["Babar"])
        self.assertEqual(names(route="not-a-number"), ["Ali", "Babar", "Chand"])
        self.assertEqual({ali.pk, babar.pk, chand.pk}, set(Customer.objects.values_list("pk", flat=True)))

    def test_supplier_city_is_on_the_add_and_edit_pages(self):
        self.client.post(reverse("supplier_add"), {"name": "Flour Mills", "city": "Gujrat"})
        supplier = Supplier.objects.get(name="Flour Mills")
        self.assertEqual(supplier.city, "Gujrat")

        response = self.client.post(reverse("supplier_edit", args=[supplier.pk]), {"name": "Flour Mills", "city": "Lahore"})

        self.assertRedirects(response, reverse("supplier_detail", args=[supplier.pk]), fetch_redirect_response=False)
        supplier.refresh_from_db()
        self.assertEqual(supplier.city, "Lahore")
        self.assertContains(self.client.get(reverse("supplier_detail", args=[supplier.pk])), "Lahore")


class TillCustomerTests(FieldSalesCase):
    def test_quick_add_keeps_the_new_fields_optional(self):
        response = self.client.post(reverse("till_customer_add"), {"name": "Walk Up Shop"}, **AJAX)

        self.assertEqual(response.status_code, 200)
        customer = Customer.objects.get(name="Walk Up Shop")
        self.assertEqual((customer.city, customer.route, customer.order_booker, customer.credit_limit), ("", None, None, None))

    def test_quick_add_takes_city_route_and_limit_and_defaults_the_order_booker(self):
        response = self.client.post(
            reverse("till_customer_add"),
            {"name": "New Shop", "city": "Mirpur A.K", "route": self.bazaar.pk, "credit_limit": "15000"},
            **AJAX,
        )

        self.assertEqual(response.status_code, 200)
        customer = Customer.objects.get(name="New Shop")
        self.assertEqual((customer.route, customer.order_booker, customer.credit_limit), (self.bazaar, self.imran, Decimal("15000.00")))

    def test_a_missing_route_and_order_booker_are_typed_in_place(self):
        response = self.client.post(
            reverse("till_customer_add"),
            {"name": "Far Shop", "city": "Kotli", "new_route": "Kotli Road", "new_order_booker": "Saad"},
            **AJAX,
        )

        self.assertEqual(response.status_code, 200)
        customer = Customer.objects.get(name="Far Shop")
        self.assertEqual((customer.route.name, customer.route.city, customer.order_booker.name), ("Kotli Road", "Kotli", "Saad"))
        self.assertEqual(list(customer.order_booker.routes.all()), [customer.route])
        # Typing a name that exists picks that one instead of making a twin.
        self.client.post(reverse("till_customer_add"), {"name": "Near Shop", "new_route": "kotli road", "new_order_booker": "SAAD"}, **AJAX)
        self.assertEqual(Route.objects.filter(name__iexact="kotli road").count(), 1)
        self.assertEqual(OrderBooker.objects.filter(name__iexact="saad").count(), 1)

    def test_a_cashier_picks_from_the_lists_but_cannot_create_master_records(self):
        cashier = get_user_model().objects.create_user(username="till", password="CashierPass123!")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)

        response = self.client.post(
            reverse("till_customer_add"), {"name": "Counter Shop", "route": self.bazaar.pk, "new_route": "Invented"}, **AJAX
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Customer.objects.get(name="Counter Shop").route, self.bazaar)
        self.assertFalse(Route.objects.filter(name="Invented").exists())

    def test_till_shows_the_limit_and_the_order_booker_beside_the_customer(self):
        customer = self.customer(order_booker=self.imran, credit_limit=Decimal("1000.00"))
        self.credit_sale(customer, (self.soap, 4))
        self.set_cart((self.soap, 1))

        page = self.client.get(reverse("pos_screen"), {"customer_id": customer.pk})
        answer = self.client.post(
            reverse("till_action", kwargs={"action": "context"}), {"customer_id": customer.pk}, **AJAX
        ).json()

        self.assertContains(page, "Credit limit Rs 1,000.00, Rs 800.00 left")
        self.assertContains(page, 'name="order_booker_id"')
        self.assertContains(page, f'<option value="{self.imran.pk}" selected>Imran</option>', html=True)
        self.assertContains(page, 'name="new_route"')
        self.assertEqual(answer["customer"]["credit_note"], "Credit limit Rs 1,000.00, Rs 800.00 left")
        self.assertFalse(answer["customer"]["credit_over"])
        self.assertEqual(answer["customer"]["order_booker_id"], str(self.imran.pk))

    def test_till_has_no_order_booker_box_while_the_module_is_off(self):
        modules.set_module_switch("field_sales", False)
        customer = self.customer(order_booker=self.imran, credit_limit=Decimal("1000.00"))

        page = self.client.get(reverse("pos_screen"), {"customer_id": customer.pk})

        self.assertNotContains(page, 'name="order_booker_id"')
        self.assertNotContains(page, 'name="route"')
        # City and the credit limit are plain customer fields: they stay.
        self.assertContains(page, 'name="city"')
        self.assertContains(page, "Credit limit Rs 1,000.00, Rs 1,000.00 left")


# =====================================================================================
# Sales: the order booker of each sale, and the credit limit at checkout
# =====================================================================================

class SaleOrderBookerTests(FieldSalesCase):
    def test_sale_takes_the_customers_order_booker(self):
        customer = self.customer(order_booker=self.imran)

        sale = self.credit_sale(customer, (self.soap, 2))

        self.assertEqual(sale.order_booker, self.imran)

    def test_the_till_can_record_another_order_booker_or_nobody_for_one_sale(self):
        customer = self.customer(order_booker=self.imran)

        other = self.credit_sale(customer, (self.soap, 1), order_booker_id=str(self.bilal.pk))
        nobody = self.credit_sale(customer, (self.soap, 1), order_booker_id="")

        self.assertEqual(other.order_booker, self.bilal)
        self.assertIsNone(nobody.order_booker)
        customer.refresh_from_db()
        self.assertEqual(customer.order_booker, self.imran)

    def test_walk_in_cash_sale_has_no_order_booker_unless_one_is_chosen(self):
        self.set_cart((self.soap, 1))
        self.checkout()
        plain = Sale.objects.order_by("-id").first()
        self.set_cart((self.soap, 1))
        self.checkout(order_booker_id=str(self.bilal.pk))
        chosen = Sale.objects.order_by("-id").first()

        self.assertIsNone(plain.order_booker)
        self.assertEqual(chosen.order_booker, self.bilal)

    def test_someone_who_left_is_not_recorded_on_new_sales(self):
        customer = self.customer(order_booker=self.imran)
        self.imran.is_active = False
        self.imran.save()

        sale = self.credit_sale(customer, (self.soap, 1))
        forced = self.credit_sale(customer, (self.soap, 1), order_booker_id=str(self.imran.pk))

        self.assertIsNone(sale.order_booker)
        self.assertIsNone(forced.order_booker)

    def test_nothing_is_recorded_while_the_module_is_off(self):
        customer = self.customer(order_booker=self.imran)
        modules.set_module_switch("field_sales", False)

        sale = self.credit_sale(customer, (self.soap, 1), order_booker_id=str(self.bilal.pk))

        self.assertIsNone(sale.order_booker)

    def test_sales_made_before_order_bookers_stay_empty(self):
        sale = Sale.objects.create(sold_by=self.admin_user, customer=self.customer(order_booker=self.imran))

        self.assertIsNone(sale.order_booker)


class CreditLimitTests(FieldSalesCase):
    def setUp(self):
        super().setUp()
        self.customer_with_limit = self.customer(credit_limit=Decimal("1000.00"))

    def test_default_is_to_refuse(self):
        self.assertEqual(SystemSettings.load().credit_limit_action, SystemSettings.CREDIT_LIMIT_REFUSE)

    def test_sale_over_the_limit_is_refused_and_says_limit_balance_and_bill(self):
        self.credit_sale(self.customer_with_limit, (self.soap, 16))  # balance 800.00
        self.set_cart((self.soap, 5))  # a bill of 250.00

        response = self.checkout(self.customer_with_limit, credit=True)

        self.assertEqual(response.status_code, 200)
        text = " ".join(self.messages(response))
        self.assertIn("Credit sale refused", text)
        self.assertIn("credit limit of Rs 1,000.00", text)
        self.assertIn("Current balance Rs 800.00", text)
        self.assertIn("this bill Rs 250.00", text)
        self.assertIn("Rs 50.00 over the limit", text)
        # Nothing was sold: one sale on the books, stock and balance as before, the bill still on screen.
        self.assertEqual(Sale.objects.count(), 1)
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.stock, Decimal("184.000"))
        self.assertEqual(get_party_balance(self.customer_with_limit), Decimal("800.00"))
        self.assertIn(str(self.soap.id), self.client.session["cart"])

    def test_the_till_gets_the_refusal_as_its_notice(self):
        self.set_cart((self.soap, 30))  # 1,500.00

        payload = self.checkout(self.customer_with_limit, credit=True, json=True).json()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "refused")
        self.assertIn("credit limit of Rs 1,000.00", payload["notice"]["text"])
        self.assertIn("this bill Rs 1,500.00", payload["notice"]["text"])
        self.assertEqual(Sale.objects.count(), 0)

    def test_a_bill_that_lands_exactly_on_the_limit_goes_through(self):
        sale = self.credit_sale(self.customer_with_limit, (self.soap, 20))  # exactly 1,000.00

        self.assertEqual(sale.total_amount, Decimal("1000.00"))
        self.assertEqual(get_party_balance(self.customer_with_limit), Decimal("1000.00"))

    def test_an_advance_makes_room_under_the_limit(self):
        create_receipt(party=self.customer_with_limit, amount=Decimal("500.00"), payment_method="cash", actor=self.admin_user)

        sale = self.credit_sale(self.customer_with_limit, (self.soap, 30))  # 1,500.00 against -500.00

        self.assertEqual(sale.total_amount, Decimal("1500.00"))
        standing = field_sales.credit_standing(self.customer_with_limit)
        self.assertEqual((standing["balance"], standing["left"], standing["is_over"]), (Decimal("1000.00"), Decimal("0.00"), False))

    def test_cash_sales_and_customers_without_a_limit_are_never_stopped(self):
        self.set_cart((self.soap, 40))
        cash = self.checkout(self.customer_with_limit)
        free = self.credit_sale(self.customer("No limit"), (self.soap, 40))

        self.assertEqual(cash.status_code, 302)
        self.assertEqual(free.total_amount, Decimal("2000.00"))
        self.assertEqual(Sale.objects.count(), 2)

    def test_a_limit_of_zero_means_no_credit_at_all(self):
        strict = self.customer("Cash only", credit_limit=Decimal("0.00"))
        self.set_cart((self.soap, 1))

        response = self.checkout(strict, credit=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("credit limit of Rs 0.00", " ".join(self.messages(response)))
        self.assertEqual(Sale.objects.count(), 0)

    def test_warn_only_lets_the_sale_through_with_a_warning(self):
        settings = SystemSettings.load()
        settings.credit_limit_action = SystemSettings.CREDIT_LIMIT_WARN
        settings.save()
        self.set_cart((self.soap, 30))

        response = self.checkout(self.customer_with_limit, credit=True)

        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(get_party_balance(self.customer_with_limit), Decimal("1500.00"))
        invoice = self.client.get(reverse("invoice", args=[sale.pk]))
        text = " ".join(self.messages(invoice))
        self.assertIn("Sold over the credit limit", text)
        self.assertIn("Rs 500.00 over the limit", text)

    def test_the_rule_is_chosen_in_settings_with_the_other_sale_rules(self):
        page = self.client.get(reverse("settings_page"))
        form = SystemSettingsForm(prefix="business", instance=SystemSettings.load())
        data = {form.add_prefix(name): form.initial.get(name, "") for name in form.fields}
        data = {key: ("on" if value is True else "" if value in (False, None) else value) for key, value in data.items()}
        data.update({"action": "save_rules", form.add_prefix("credit_limit_action"): "warn"})

        response = self.client.post(reverse("settings_page"), data)

        self.assertContains(page, "business-credit_limit_action")
        self.assertContains(page, "Warn only")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SystemSettings.load().credit_limit_action, SystemSettings.CREDIT_LIMIT_WARN)

    def test_a_settings_post_without_the_choice_keeps_it(self):
        settings = SystemSettings.load()
        settings.credit_limit_action = SystemSettings.CREDIT_LIMIT_WARN
        settings.save()

        form = SystemSettingsForm({"business-tax_mode": "manual_cart", "business-tax_label": "GST", "business-default_sale_payment_method": "cash", "business-thermal_printer_width_mm": "50"}, prefix="business", instance=SystemSettings.load())

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().credit_limit_action, SystemSettings.CREDIT_LIMIT_WARN)

    def test_standing_and_note(self):
        self.credit_sale(self.customer_with_limit, (self.soap, 4))
        standing = field_sales.credit_standing(self.customer_with_limit)

        self.assertEqual((standing["limit"], standing["balance"], standing["left"]), (Decimal("1000.00"), Decimal("200.00"), Decimal("800.00")))
        self.assertEqual(field_sales.credit_limit_note(standing), "Credit limit Rs 1,000.00, Rs 800.00 left")
        over = field_sales.credit_standing(self.customer_with_limit, Decimal("1250.00"))
        self.assertEqual(field_sales.credit_limit_note(over), "Credit limit Rs 1,000.00, over by Rs 250.00")
        self.assertEqual(field_sales.credit_limit_note(field_sales.credit_standing(self.customer("Free"))), "")


# =====================================================================================
# The customer ledger laid out as a statement
# =====================================================================================

class CustomerLedgerTests(FieldSalesCase):
    def setUp(self):
        super().setUp()
        self.customer_obj = self.customer(city="Mirpur A.K", route=self.bazaar, order_booker=self.imran)
        # 20 kg flour + 5 soap on credit: 3,250.00. Then 2 kg come back, then 1,000.00 is paid.
        self.sale = self.credit_sale(self.customer_obj, (self.flour, "20"), (self.soap, 5))
        self.flour_line = SaleItem.objects.get(sale=self.sale, product=self.flour)
        self.sale_return = create_sale_return(
            sale=self.sale, quantities={self.flour_line.id: "2"}, refund_method="receivable", reason="Damp", actor=self.admin_user
        )
        create_receipt(party=self.customer_obj, amount=Decimal("1000.00"), payment_method="cash", actor=self.admin_user)

    def test_invoice_quantity_is_the_sum_of_the_lines_and_negative_for_a_return(self):
        self.assertEqual(invoice_quantity(self.sale), Decimal("25.000"))
        self.assertEqual(invoice_quantity(self.sale_return), Decimal("-2.000"))

    def test_statement_rows_carry_quantity_debit_credit_and_a_running_balance(self):
        data = get_party_statement(party=self.customer_obj)

        rows = [(row["document"], row["quantity"], row["debit"], row["credit"], row["running_balance"]) for row in data["rows"]]
        self.assertEqual(
            rows,
            [
                (f"Invoice {self.sale.invoice_number}", Decimal("25.000"), Decimal("3250.00"), Decimal("0.00"), Decimal("3250.00")),
                (f"Sale return #{self.sale_return.id}", Decimal("-2.000"), Decimal("0.00"), Decimal("300.00"), Decimal("2950.00")),
                (data["rows"][2]["document"], None, Decimal("0.00"), Decimal("1000.00"), Decimal("1950.00")),
            ],
        )
        self.assertTrue(data["rows"][2]["document"].startswith("Receipt #"))
        self.assertEqual(data["total_quantity"], Decimal("23.000"))
        self.assertEqual(data["closing_balance"], get_party_balance(self.customer_obj))

    def test_balance_goes_negative_when_the_customer_has_paid_in_advance(self):
        create_receipt(party=self.customer_obj, amount=Decimal("5000.00"), payment_method="cash", actor=self.admin_user)

        data = get_party_statement(party=self.customer_obj)

        self.assertEqual(data["rows"][-1]["running_balance"], Decimal("-3050.00"))
        self.assertEqual(data["closing_balance"], Decimal("-3050.00"))
        self.assertEqual(data["balance_type"], "advance")
        self.assertEqual(get_party_balance(self.customer_obj), Decimal("-3050.00"))

    def test_api_gives_the_page_quantities_period_and_totals(self):
        payload = self.client.get(reverse("party_statement_api"), {"customer_id": self.customer_obj.pk}).json()

        self.assertEqual([row["quantity"] for row in payload["rows"]], ["25.000", "-2.000", None])
        self.assertEqual(payload["total_quantity"], "23.000")
        self.assertEqual(payload["closing_balance"], "1950.00")
        self.assertIsNone(payload["from_date"])

        tomorrow = (self.today + datetime.timedelta(days=1)).isoformat()
        later = self.client.get(reverse("party_statement_api"), {"customer_id": self.customer_obj.pk, "from": tomorrow}).json()
        self.assertEqual((later["from_date"], later["opening_balance"], later["closing_balance"], later["rows"]), (tomorrow, "1950.00", "1950.00", []))
        self.assertEqual(later["total_quantity"], "0.000")

    def test_supplier_statement_is_untouched_by_quantities(self):
        supplier = Supplier.objects.create(name="Flour Mills")

        payload = self.client.get(reverse("party_statement_api"), {"supplier_id": supplier.pk}).json()

        self.assertEqual((payload["rows"], payload["total_quantity"]), ([], "0.000"))

    def test_customer_page_lays_the_ledger_out_as_a_statement(self):
        response = self.client.get(reverse("customer_detail", args=[self.customer_obj.pk]))

        self.assertContains(response, '<th scope="col" class="k-num" data-k="meta">Qty</th>', html=True)
        self.assertContains(response, "Reference")
        self.assertContains(response, "statement: true")
        self.assertContains(response, "quantityLabel: 'Qty'")
        self.assertContains(response, "Customer ledger: Ali Traders")
        # The supplier page keeps its own ledger.
        supplier = Supplier.objects.create(name="Flour Mills")
        self.assertNotContains(self.client.get(reverse("supplier_detail", args=[supplier.pk])), "statement: true")

    def test_printable_statement_has_the_header_columns_totals_and_closing_balance(self):
        response = self.client.get(reverse("party_statement", args=["customer", self.customer_obj.pk]))

        self.assertEqual(response.status_code, 200)
        header = response.context["statement_header"]
        self.assertEqual((header["name"], header["city"], header["route"], header["order_booker"]), ("Ali Traders", "Mirpur A.K", "Main Bazaar", "Imran"))
        rows = response.context["statement_rows"]
        self.assertEqual([row["quantity"] for row in rows], [Decimal("25.000"), Decimal("-2.000"), None])
        self.assertEqual([row["signed_balance"] for row in rows], [Decimal("3250.00"), Decimal("2950.00"), Decimal("1950.00")])
        self.assertEqual(rows[0]["reference"], f"Invoice {self.sale.invoice_number}")
        self.assertEqual(response.context["total_quantity"], Decimal("23.000"))
        self.assertEqual((response.context["total_debit"], response.context["total_credit"]), (Decimal("3250.00"), Decimal("1300.00")))
        self.assertEqual(response.context["signed_closing"], get_party_balance(self.customer_obj))
        for text in ("City : Mirpur A.K", "Route : Main Bazaar", "Order booker : Imran", "Reference / description", "Qty", "Total for the period", "1,950.00"):
            self.assertContains(response, text)
        # The table reads like the printed statement: whole rupees with thousands separators.
        self.assertContains(response, 'data-k="balance">3,250</td>')
        self.assertContains(response, 'data-k-suffix="Cr">1,300</td>')

    def test_printable_statement_for_a_period_opens_with_the_balance_brought_forward(self):
        yesterday = self.today - datetime.timedelta(days=1)
        set_party_opening_balance(party=self.customer_obj, amount=Decimal("400.00"), direction=THEY_OWE_US, date=yesterday, actor=self.admin_user)

        response = self.client.get(
            reverse("party_statement", args=["customer", self.customer_obj.pk]), {"start": self.today.isoformat(), "end": self.today.isoformat()}
        )

        self.assertEqual(response.context["signed_opening"], Decimal("400.00"))
        self.assertEqual(response.context["signed_closing"], Decimal("2350.00"))
        self.assertEqual(response.context["signed_closing"], get_party_balance(self.customer_obj))
        self.assertContains(response, f"Opening balance on {self.today:%d %b %Y}")
        self.assertEqual(len(response.context["statement_rows"]), 3)

    def test_printable_statement_shows_an_advance_as_a_minus(self):
        create_receipt(party=self.customer_obj, amount=Decimal("5000.00"), payment_method="cash", actor=self.admin_user)

        response = self.client.get(reverse("party_statement", args=["customer", self.customer_obj.pk]))

        self.assertEqual(response.context["signed_closing"], Decimal("-3050.00"))
        self.assertContains(response, ">-3,050</td>")

    def test_statement_amounts_drop_only_empty_paisa(self):
        from pos.templatetags.field_sales_tags import statement_amount

        self.assertEqual(
            [statement_amount(value) for value in (Decimal("12500"), Decimal("12500.50"), Decimal("-3050.00"), Decimal("0"))],
            ["12,500", "12,500.50", "-3,050", "0"],
        )

    def test_csv_export_has_the_same_statement(self):
        lines = read_csv(self.client.get(reverse("party_statement", args=["customer", self.customer_obj.pk]), {"export": "csv"}))

        self.assertEqual(lines[0], ["Date", "Reference", "Description", "Qty", "Debit", "Credit", "Balance"])
        labelled = {line[1]: line for line in lines[1:]}
        self.assertEqual(labelled["Customer"][2], "Ali Traders")
        self.assertEqual(labelled["City"][2], "Mirpur A.K")
        self.assertEqual(labelled["Route"][2], "Main Bazaar")
        self.assertEqual(labelled["Order booker"][2], "Imran")
        self.assertEqual(labelled["Opening balance"][6], "0.00")
        invoice = labelled[f"Invoice {self.sale.invoice_number}"]
        self.assertEqual((invoice[0], invoice[3], invoice[4], invoice[5], invoice[6]), (self.today.isoformat(), "25", "3250.00", "", "3250.00"))
        returned = labelled[f"Sale return #{self.sale_return.id}"]
        self.assertEqual((returned[3], returned[4], returned[5], returned[6]), ("-2", "", "300.00", "2950.00"))
        self.assertEqual(labelled["Closing balance"][3:], ["23", "3250.00", "1300.00", "1950.00"])

    def test_supplier_statement_and_its_csv_keep_their_layout(self):
        supplier = Supplier.objects.create(name="Flour Mills")
        url = reverse("party_statement", args=["supplier", supplier.pk])

        page = self.client.get(url)
        lines = read_csv(self.client.get(url, {"export": "csv"}))

        self.assertNotIn("customer_statement", page.context)
        self.assertEqual(lines[0], ["Date", "Voucher", "Type", "Narration", "Debit", "Credit", "Balance", "Dr/Cr"])

    def test_with_the_module_off_the_statement_names_only_the_city(self):
        modules.set_module_switch("field_sales", False)

        response = self.client.get(reverse("party_statement", args=["customer", self.customer_obj.pk]))

        header = response.context["statement_header"]
        self.assertEqual((header["city"], header["route"], header["order_booker"]), ("Mirpur A.K", "", ""))
        self.assertNotContains(response, "Route : Main Bazaar")


# =====================================================================================
# Reports
# =====================================================================================

class ReceivablesByAreaTests(FieldSalesCase):
    def setUp(self):
        super().setUp()
        self.ali = self.customer("Ali", city="Mirpur A.K", route=self.bazaar, order_booker=self.imran, credit_limit=Decimal("5000.00"))
        self.babar = self.customer("Babar", city="Mirpur A.K", route=self.bazaar, order_booker=self.imran)
        self.chand = self.customer("Chand", city="Dadyal", route=self.canal, order_booker=self.bilal)
        self.dawood = self.customer("Dawood")
        self.customer("Ehsan", city="Dadyal")  # nothing owed either way
        self.credit_sale(self.ali, (self.soap, 10))  # 500
        self.credit_sale(self.babar, (self.soap, 4))  # 200
        self.credit_sale(self.chand, (self.soap, 6))  # 300
        create_receipt(party=self.dawood, amount=Decimal("150.00"), payment_method="cash", actor=self.admin_user)  # advance

    def shape(self, report):
        return [(row.get("_kind", ""), row["customer"], row.get("balance")) for row in report.rows]

    def test_customers_are_grouped_by_city_with_a_subtotal_for_each(self):
        report = build_receivables_by_area(None, self.today, {})

        self.assertEqual(
            self.shape(report),
            [
                ("group", "City : Dadyal", None),
                ("", "Chand", Decimal("300.00")),
                ("subtotal", "Total Dadyal", Decimal("300.00")),
                ("group", "City : Mirpur A.K", None),
                ("", "Ali", Decimal("500.00")),
                ("", "Babar", Decimal("200.00")),
                ("subtotal", "Total Mirpur A.K", Decimal("700.00")),
                ("group", "City : No city", None),
                ("", "Dawood", Decimal("-150.00")),
                ("subtotal", "Total No city", Decimal("-150.00")),
            ],
        )
        self.assertEqual(report.totals["balance"], Decimal("850.00"))
        cards = {card["label"]: card["value"] for card in report.cards}
        self.assertEqual((cards["Customers"], cards["Customers Owe You"], cards["Advances You Hold"], cards["Net Receivable"]), (4, Decimal("1000.00"), Decimal("150.00"), Decimal("850.00")))

    def test_every_balance_is_the_balance_the_customer_page_shows(self):
        report = build_receivables_by_area(None, self.today, {})

        for row in report.rows:
            if not row.get("_kind"):
                customer = Customer.objects.get(name=row["customer"])
                self.assertEqual(row["balance"], get_party_balance(customer))

    def test_grouping_by_route_and_by_order_booker(self):
        by_route = build_receivables_by_area(None, self.today, {"group": "route"})
        by_booker = build_receivables_by_area(None, self.today, {"group": "order_booker"})

        self.assertEqual(
            [row["customer"] for row in by_route.rows if row.get("_kind") == "group"],
            ["Route : Canal Road (Dadyal)", "Route : Main Bazaar (Mirpur A.K)", "Route : No route"],
        )
        self.assertEqual(
            [(row["customer"], row["balance"]) for row in by_booker.rows if row.get("_kind") == "subtotal"],
            [("Total Bilal", Decimal("300.00")), ("Total Imran", Decimal("700.00")), ("Total No order booker", Decimal("-150.00"))],
        )

    def test_show_only_who_owes_or_everyone(self):
        owing = build_receivables_by_area(None, self.today, {"show": "owing"})
        everyone = build_receivables_by_area(None, self.today, {"show": "everyone"})

        self.assertNotIn("Dawood", [row["customer"] for row in owing.rows])
        self.assertEqual(owing.totals["balance"], Decimal("1000.00"))
        self.assertIn("Ehsan", [row["customer"] for row in everyone.rows])

    def test_as_of_an_earlier_day_leaves_later_entries_out(self):
        yesterday = self.today - datetime.timedelta(days=1)

        report = build_receivables_by_area(None, yesterday, {})

        self.assertEqual(report.rows, [])
        self.assertEqual(report.totals, {})

    def test_page_and_csv(self):
        url = reverse("report_receivables_by_area")

        page = self.client.get(url)
        lines = read_csv(self.client.get(url, {"export": "csv"}))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "City : Mirpur A.K")
        self.assertContains(page, "Total Mirpur A.K")
        self.assertContains(page, 'class="rp-group-row k-row-plain"')
        self.assertEqual(lines[0], ["Customer", "Phone", "City", "Route", "Order Booker", "Credit Limit", "Balance"])
        # Group headings are left out of the file; every customer line names its own city.
        self.assertNotIn("City : Mirpur A.K", [line[0] for line in lines])
        self.assertIn(["Ali", "", "Mirpur A.K", "Main Bazaar", "Imran", "5000.00", "500.00"], lines)
        self.assertIn(["Total Mirpur A.K", "2 customers", "", "", "", "", "700.00"], lines)
        self.assertEqual(lines[-1], ["Total", "", "", "", "", "", "850.00"])

    def test_with_the_module_off_it_still_groups_by_city_only(self):
        modules.set_module_switch("field_sales", False)

        report = build_receivables_by_area(None, self.today, {"group": "route"})

        self.assertEqual([column.key for column in report.columns], ["customer", "phone", "city", "credit_limit", "balance"])
        self.assertEqual(report.filters[0]["options"], [("city", "City")])
        self.assertEqual(report.rows[0]["customer"], "City : Dadyal")
        self.assertEqual(self.client.get(reverse("report_receivables_by_area")).status_code, 200)


class SalesByOrderBookerTests(FieldSalesCase):
    def setUp(self):
        super().setUp()
        self.ali = self.customer("Ali", order_booker=self.imran)
        self.babar = self.customer("Babar", order_booker=self.bilal)
        self.first = self.credit_sale(self.ali, (self.flour, "10"), (self.soap, 2))  # 1,600.00, qty 12
        self.credit_sale(self.ali, (self.soap, 4))  # 200.00, qty 4
        self.credit_sale(self.babar, (self.soap, 6))  # 300.00, qty 6
        self.set_cart((self.soap, 1))
        self.checkout()  # a walk-in cash sale: 50.00, qty 1, nobody recorded
        line = SaleItem.objects.get(sale=self.first, product=self.flour)
        create_sale_return(sale=self.first, quantities={line.id: "1"}, refund_method="receivable", reason="Torn bag", actor=self.admin_user)

    def test_each_order_booker_gets_invoices_quantity_and_sales_net_of_returns(self):
        report = build_sales_by_order_booker(self.today, self.today, {})

        rows = {row["booker"]: row for row in report.rows}
        self.assertEqual([row["booker"] for row in report.rows], ["Imran", "Bilal", "No order booker"])
        imran = rows["Imran"]
        self.assertEqual((imran["invoices"], imran["customers"], imran["quantity"]), (2, 1, "15"))
        self.assertEqual((imran["billed"], imran["returns"], imran["net_sales"]), (Decimal("1800.00"), Decimal("150.00"), Decimal("1650.00")))
        self.assertEqual((imran["paid"], imran["due"]), (Decimal("0.00"), Decimal("1650.00")))
        self.assertEqual((rows["Bilal"]["invoices"], rows["Bilal"]["quantity"], rows["Bilal"]["net_sales"]), (1, "6", Decimal("300.00")))
        nobody = rows["No order booker"]
        self.assertEqual((nobody["invoices"], nobody["quantity"], nobody["billed"], nobody["paid"]), (1, "1", Decimal("50.00"), Decimal("50.00")))
        self.assertEqual((report.totals["invoices"], report.totals["quantity"], report.totals["net_sales"]), (4, "22", Decimal("2000.00")))

    def test_a_sale_stays_with_the_order_booker_it_was_made_under(self):
        self.ali.order_booker = self.bilal
        self.ali.save()

        report = build_sales_by_order_booker(self.today, self.today, {})

        self.assertEqual({row["booker"]: row["invoices"] for row in report.rows}, {"Imran": 2, "Bilal": 1, "No order booker": 1})

    def test_other_dates_are_empty(self):
        yesterday = self.today - datetime.timedelta(days=1)

        report = build_sales_by_order_booker(yesterday, yesterday, {})

        self.assertEqual((report.rows, report.totals), ([], {}))

    def test_page_and_csv(self):
        url = reverse("report_sales_by_order_booker")

        page = self.client.get(url)
        lines = read_csv(self.client.get(url, {"export": "csv"}))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Sales by Order Booker")
        self.assertEqual(lines[0], ["Order Booker", "Invoices", "Customers", "Qty", "Billed", "Returns", "Net Sales", "Paid", "Due on Invoices"])
        self.assertEqual(lines[1], ["Imran", "2", "1", "15", "1800.00", "150.00", "1650.00", "0.00", "1650.00"])
        self.assertEqual(lines[-1][0], "Total")
        self.assertEqual(lines[-1][3], "22")


# =====================================================================================
# The feature module
# =====================================================================================

class FieldSalesModuleTests(FieldSalesCase):
    def test_module_is_listed_in_settings_and_is_on_by_default(self):
        response = self.client.get(reverse("settings_page"))

        rows = {row["key"]: row for group in response.context["feature_groups"] for row in group["rows"]}
        self.assertIn("field_sales", rows)
        self.assertTrue(rows["field_sales"]["enabled"])
        self.assertContains(response, "Routes and order bookers")
        self.assertTrue(field_sales.field_sales_enabled())

    def test_switching_it_off_closes_the_screens_and_hides_the_links_but_keeps_the_data(self):
        customer = self.customer(city="Mirpur A.K", route=self.bazaar, order_booker=self.imran, credit_limit=Decimal("1000.00"))
        modules.set_module_switch("field_sales", False)

        self.assertFalse(field_sales.field_sales_enabled())
        for name in ("route_list", "route_add", "order_booker_list", "order_booker_add"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)
        self.assertEqual(self.client.post(reverse("quick_create_route"), {"quick_route-name": "X"}, **AJAX).status_code, 403)
        self.assertEqual(self.client.get(reverse("report_sales_by_order_booker")).status_code, 302)

        customers = self.client.get(reverse("customer_list"))
        detail = self.client.get(reverse("customer_detail", args=[customer.pk]))
        add = self.client.get(reverse("customer_add"))
        self.assertNotContains(customers, reverse("route_list"))
        self.assertNotContains(customers, "Main Bazaar")
        self.assertContains(customers, "Mirpur A.K")
        self.assertNotContains(detail, "Order booker")
        self.assertContains(detail, "Credit limit")
        self.assertNotContains(add, 'name="route"')
        self.assertContains(add, 'name="credit_limit"')

        customer.refresh_from_db()
        self.assertEqual((customer.route, customer.order_booker), (self.bazaar, self.imran))
        modules.set_module_switch("field_sales", True)
        self.assertEqual(self.client.get(reverse("route_list")).status_code, 200)

    def test_reports_follow_the_modules(self):
        def slugs():
            return {link["slug"] for group in report_navigation() for link in group["links"]}

        self.assertTrue({"sales-by-order-booker", "receivables-by-area"} <= slugs())
        modules.set_module_switch("field_sales", False)
        self.assertNotIn("sales-by-order-booker", slugs())
        self.assertIn("receivables-by-area", slugs())
        modules.set_module_switch("parties", False)
        self.assertNotIn("receivables-by-area", slugs())

    def test_routes_and_order_bookers_need_the_customers_module(self):
        modules.set_module_switch("parties", False)

        self.assertFalse(field_sales.field_sales_enabled())
        self.assertEqual(self.client.get(reverse("route_list")).status_code, 302)
