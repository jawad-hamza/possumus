"""Cross-module behaviour wired up during integration: service items, team limits,
team password resets, paginated purchases, opening balances on new parties."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from accounts.models import AuditLog, PartyLedgerEntry, Settlement, Voucher, VoucherLine
from accounts.services.party_balance import get_party_balance
from pos.forms import CompanyProfileForm, ProductForm
from pos.models import (
    Customer,
    Product,
    Purchase,
    PurchaseItem,
    Sale,
    Supplier,
    UserPreference,
    trial_days,
)
from pos.services.access import CASHIER_ROLE, assign_role
from pos.services.purchase_balances import annotate_purchase_balances, outstanding_purchase_total
from pos.services.stock_control import create_stock_adjustment
from pos.test_support import AuthenticatedAdminTestCase


def _cart_line(product, quantity="1"):
    return {
        "name": product.name,
        "price": str(product.price),
        "original_price": str(product.price),
        "discount_percent": "0.00",
        "quantity": str(quantity),
    }


class ServiceItemTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.service = Product.objects.create(
            name="Delivery", sku="SRV-DEL", cost_price=Decimal("40.00"), price=Decimal("150.00"), stock=0, track_stock=False
        )

    def _checkout(self, product, quantity="1"):
        session = self.client.session
        session["cart"] = {str(product.id): _cart_line(product, quantity)}
        session.save()
        return self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"},
        )

    def test_selling_a_service_moves_no_stock_and_books_no_cost_of_goods(self):
        response = self._checkout(self.service, "3")

        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        item = sale.items.get()
        self.assertEqual(item.cost_price, Decimal("0"))
        self.service.refresh_from_db()
        self.assertEqual(self.service.stock, Decimal("0"))
        voucher = Voucher.objects.get(voucher_type=Voucher.SALE, ref_id=sale.id)
        titles = set(VoucherLine.objects.filter(voucher=voucher).values_list("account__title", flat=True))
        self.assertNotIn("Inventory", titles)
        self.assertNotIn("Cost of Goods Sold", titles)

    def test_sale_voucher_narration_uses_the_invoice_number(self):
        self._checkout(self.service)

        sale = Sale.objects.get()
        voucher = Voucher.objects.get(voucher_type=Voucher.SALE, ref_id=sale.id)
        self.assertTrue(sale.invoice_number)
        self.assertIn(sale.invoice_number, voucher.narration)

    def test_service_is_clickable_on_the_pos_screen_and_has_no_quantity_ceiling(self):
        page = self.client.get(reverse("pos_screen"))
        self.assertContains(page, "Not tracked")
        self.assertNotContains(page, "Out of Stock")

        self.client.post(reverse("pos_screen"), {"action": "add", "product_id": self.service.id})
        page = self.client.get(reverse("pos_screen"))
        self.assertIsNone(page.context["cart_rows"][0]["stock"])
        self.assertEqual(page.context["cart_rows"][0]["stock_error"], "")

    def test_service_cannot_be_purchased_into_inventory(self):
        purchase = Purchase.objects.create()
        line = PurchaseItem(purchase=purchase, product=self.service, quantity=1, cost_price=Decimal("40"), sale_price=Decimal("150"))

        with self.assertRaises(ValidationError):
            line.save()

    def test_service_cannot_be_stock_adjusted(self):
        with self.assertRaisesMessage(ValueError, "does not track stock"):
            create_stock_adjustment(
                product=self.service, adjustment_mode="adjustment", reason="count", quantity_delta=Decimal("1")
            )

    def test_service_is_left_out_of_low_stock(self):
        response = self.client.get(reverse("low_stock_list"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "SRV-DEL")

    def test_product_form_without_the_item_type_field_keeps_counting_stock(self):
        form = ProductForm(
            data={"name": "Tea", "sku": "TEA-9", "cost_price": "10", "price": "20", "minimum_margin_percent": "0",
                  "low_stock_threshold": "5", "is_active": "on", "opening_stock": "4"}
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.save(commit=False).track_stock)

    def test_product_form_refuses_opening_stock_for_a_service(self):
        form = ProductForm(
            data={"name": "Repair", "sku": "SRV-REP", "cost_price": "0", "price": "500", "minimum_margin_percent": "0",
                  "low_stock_threshold": "0", "is_active": "on", "opening_stock": "4", "track_stock": "0"}
        )

        self.assertFalse(form.is_valid())
        self.assertIn("opening_stock", form.errors)

    def test_stocked_item_with_quantity_cannot_be_switched_to_service(self):
        product = Product.objects.create(name="Rice", sku="RICE-1", cost_price=Decimal("5"), price=Decimal("9"), stock=3)
        form = ProductForm(
            data={"name": "Rice", "sku": "RICE-1", "cost_price": "5", "price": "9", "minimum_margin_percent": "0",
                  "low_stock_threshold": "1", "is_active": "on", "track_stock": "0"},
            instance=product,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("track_stock", form.errors)


class PurchaseLineCostTests(AuthenticatedAdminTestCase):
    def test_second_line_for_the_same_product_averages_against_the_first(self):
        product = Product.objects.create(name="Sugar", sku="SUG-1", cost_price=Decimal("0"), price=Decimal("30"), stock=0, unit="kg")
        purchase = Purchase.objects.create()
        PurchaseItem.objects.create(purchase=purchase, product=product, quantity=Decimal("10"), cost_price=Decimal("10"), sale_price=Decimal("30"))
        # A stale in-memory copy of the product, as a second form line would carry.
        stale = Product.objects.get(pk=product.pk)
        stale.cost_price = Decimal("0")
        PurchaseItem.objects.create(purchase=purchase, product=stale, quantity=Decimal("10"), cost_price=Decimal("20"), sale_price=Decimal("30"))

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("20"))
        self.assertEqual(product.cost_price, Decimal("15.00"))


class PurchaseListPaginationTests(AuthenticatedAdminTestCase):
    def test_list_is_paginated_and_outstanding_matches_the_per_bill_figures(self):
        supplier = Supplier.objects.create(name="Mills")
        other = Supplier.objects.create(name="Dairy")
        for index in range(30):
            Purchase.objects.create(
                supplier=supplier if index % 2 else other,
                total_amount=Decimal("100.00"),
                paid_amount=Decimal("40.00") if index % 3 else Decimal("100.00"),
                supplier_invoice_number=f"B-{index}",
            )
        from django.contrib.contenttypes.models import ContentType

        Settlement.objects.create(
            settlement_type=Settlement.PAYMENT,
            content_type=ContentType.objects.get_for_model(Supplier),
            object_id=supplier.id,
            amount=Decimal("250.00"),
            payment_method="cash",
        )

        rows = annotate_purchase_balances(Purchase.objects.all())
        expected = sum((row.report_balance_due for row in rows), Decimal("0"))
        self.assertEqual(outstanding_purchase_total(), expected)

        response = self.client.get(reverse("purchase_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["purchases"]), 25)
        self.assertEqual(response.context["purchase_count"], 30)
        self.assertEqual(response.context["outstanding_total"], expected)
        self.assertContains(response, "Bill B-29")
        second = self.client.get(reverse("purchase_list"), {"page": 2})
        self.assertEqual(len(second.context["purchases"]), 5)


class TeamControlsTests(AuthenticatedAdminTestCase):
    def _team_payload(self, **overrides):
        payload = {
            "action": "create_user",
            "team-username": "till2",
            "team-password1": "Sturdy-Pass-9911",
            "team-password2": "Sturdy-Pass-9911",
            "team-role": CASHIER_ROLE,
            "team-is_active": "on",
        }
        payload.update(overrides)
        return payload

    def test_licence_user_limit_blocks_a_new_active_login(self):
        with mock.patch("pos.views.settings_views.team_user_limit_message", return_value="This licence allows 1 active user."):
            response = self.client.post(reverse("settings_page"), self._team_payload())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This licence allows 1 active user.")
        self.assertFalse(get_user_model().objects.filter(username="till2").exists())

    def test_licence_user_limit_blocks_enabling_but_not_disabling(self):
        member = get_user_model().objects.create_user(username="till3", password="Sturdy-Pass-9911", is_active=False)
        with mock.patch("pos.views.settings_views.team_user_limit_message", return_value="Limit reached."):
            self.client.post(reverse("settings_page"), {"action": "toggle_user", "user_id": member.id})
        member.refresh_from_db()
        self.assertFalse(member.is_active)

        member.is_active = True
        member.save(update_fields=["is_active"])
        with mock.patch("pos.views.settings_views.team_user_limit_message", return_value="Limit reached."):
            self.client.post(reverse("settings_page"), {"action": "toggle_user", "user_id": member.id})
        member.refresh_from_db()
        self.assertFalse(member.is_active)

    def test_admin_sets_a_new_password_for_a_cashier(self):
        cashier = get_user_model().objects.create_user(username="cash1", password="Old-Pass-771100")
        assign_role(cashier, CASHIER_ROLE)

        response = self.client.post(
            reverse("settings_page"),
            {"action": "set_user_password", "user_id": cashier.id, "new_password": "Fresh-Pass-552200"},
        )

        self.assertRedirects(response, reverse("settings_page"))
        cashier.refresh_from_db()
        self.assertTrue(cashier.check_password("Fresh-Pass-552200"))
        self.assertTrue(AuditLog.objects.filter(action="team_user_password_reset").exists())

    def test_weak_password_is_refused(self):
        cashier = get_user_model().objects.create_user(username="cash2", password="Old-Pass-771100")

        response = self.client.post(
            reverse("settings_page"), {"action": "set_user_password", "user_id": cashier.id, "new_password": "123"}
        )

        self.assertEqual(response.status_code, 200)
        cashier.refresh_from_db()
        self.assertTrue(cashier.check_password("Old-Pass-771100"))

    def test_superuser_password_cannot_be_set_by_another_admin(self):
        boss = get_user_model().objects.create_superuser(username="boss", password="Boss-Pass-771100", email="b@example.com")

        self.client.post(
            reverse("settings_page"), {"action": "set_user_password", "user_id": boss.id, "new_password": "Fresh-Pass-552200"}
        )

        boss.refresh_from_db()
        self.assertTrue(boss.check_password("Boss-Pass-771100"))

    def test_own_password_goes_through_the_change_password_page(self):
        response = self.client.post(
            reverse("settings_page"),
            {"action": "set_user_password", "user_id": self.admin_user.id, "new_password": "Fresh-Pass-552200"},
        )

        self.assertRedirects(response, reverse("password_change"))


class PartyOpeningOnAddTests(AuthenticatedAdminTestCase):
    def test_customer_is_added_with_what_they_already_owe(self):
        response = self.client.post(
            reverse("customer_add"),
            {"name": "Old Client", "opening-amount": "750.00", "opening-direction": "receivable", "opening-date": ""},
        )

        customer = Customer.objects.get(name="Old Client")
        self.assertRedirects(response, reverse("customer_detail", args=[customer.id]))
        self.assertEqual(get_party_balance(customer), Decimal("750.00"))

    def test_supplier_is_added_with_what_is_already_owed_to_them(self):
        self.client.post(reverse("supplier_add"), {"name": "Old Mill", "opening-amount": "300", "opening-direction": "payable"})

        supplier = Supplier.objects.get(name="Old Mill")
        self.assertEqual(get_party_balance(supplier), Decimal("-300.00"))

    def test_plain_add_posts_nothing_to_the_books(self):
        self.client.post(reverse("customer_add"), {"name": "Walk Regular"})

        self.assertTrue(Customer.objects.filter(name="Walk Regular").exists())
        self.assertFalse(PartyLedgerEntry.objects.exists())

    def test_cashier_is_not_offered_the_opening_balance(self):
        cashier = get_user_model().objects.create_user(username="cash9", password="Old-Pass-771100")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)

        page = self.client.get(reverse("customer_add"))
        self.assertIsNone(page.context["opening_form"])
        self.client.post(reverse("customer_add"), {"name": "Sneaky", "opening-amount": "999", "opening-direction": "receivable"})

        self.assertTrue(Customer.objects.filter(name="Sneaky").exists())
        self.assertFalse(PartyLedgerEntry.objects.exists())


class CopyAndPlumbingTests(AuthenticatedAdminTestCase):
    def test_public_pages_quote_the_configured_trial_length(self):
        self.client.logout()
        with override_settings(TRIAL_DAYS=10):
            self.assertEqual(trial_days(), 10)
            page = self.client.get(reverse("login"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "3 free days")

    def test_logo_over_the_size_limit_is_refused(self):
        big = SimpleUploadedFile("logo.png", b"x" * 2048, content_type="image/png")
        with override_settings(MAX_LOGO_UPLOAD_BYTES=1024):
            form = CompanyProfileForm(data={"name": "Shop"}, files={"logo": big})
            self.assertFalse(form.is_valid())
        self.assertIn("logo", form.errors)

    def test_error_messages_render_as_bootstrap_danger_alerts(self):
        response = self.client.post(
            reverse("settings_page"), {"action": "toggle_user", "user_id": self.admin_user.id}, follow=True
        )

        self.assertContains(response, "alert-danger")
        self.assertNotContains(response, "alert-error")

    def test_json_request_from_a_signed_out_till_is_told_where_to_go(self):
        self.client.logout()
        response = self.client.post(
            reverse("scan_barcode", args=["12345"]), HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["redirect"], reverse("login"))

    def test_google_account_id_is_stored_on_the_preference(self):
        from pos.services.google_auth import find_user_by_google_sub, remember_google_sub

        self.assertTrue(remember_google_sub(self.admin_user, "sub-123"))
        self.assertEqual(UserPreference.objects.get(user=self.admin_user).google_sub, "sub-123")
        self.assertEqual(find_user_by_google_sub("sub-123"), self.admin_user)

    def test_static_assets_are_served_from_the_app_not_a_cdn(self):
        page = self.client.get(reverse("dashboard"))

        self.assertNotContains(page, "cdn.jsdelivr.net")
        self.assertContains(page, "vendor/bootstrap/css/bootstrap.min")
