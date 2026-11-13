"""Switchable feature modules: the licence sets the ceiling, the shop toggles within it."""
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pos.forms import ProductForm, SystemSettingsForm
from pos.models import CompanyProfile, License, OwnerOnboarding, Product, SystemSettings
from pos.services import licensing, modules
from pos.services.access import (
    ADMIN_ROLE,
    CASHIER_ROLE,
    assign_role,
    ensure_role_groups,
    get_navigation_access,
    user_can_access_module,
)
from pos.services.reporting import report_navigation

# A throwaway key pair for the whole module. The real licensing/private_key.pem is never read here.
TEST_PRIVATE_PEM, TEST_PUBLIC_PEM = licensing.generate_keypair()
TEST_PRIVATE_KEY = serialization.load_pem_private_key(TEST_PRIVATE_PEM, password=None)
AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def issue_key(**overrides):
    options = {"licensee": "Corner Shop", "email": "owner@example.com", "plan": "desktop", "days": 365}
    options.update(overrides)
    return licensing.sign_license(licensing.build_payload(**options), TEST_PRIVATE_KEY)


def make_owner(*, trial_ended=False, username="owner@example.com"):
    ensure_role_groups()
    user = get_user_model().objects.create_user(
        username=username, email=username, password="OwnerPass123!", is_active=True, is_staff=True
    )
    assign_role(user, ADMIN_ROLE)
    now = timezone.now()
    started = now - timedelta(days=30) if trial_ended else now
    OwnerOnboarding.objects.create(
        user=user,
        email_verified=True,
        email_verified_at=started,
        business_created_at=started,
        trial_started_at=started,
        trial_ends_at=started + timedelta(days=10),
        billing_state=OwnerOnboarding.BILLING_PENDING if trial_ended else OwnerOnboarding.BILLING_TRIAL,
    )
    if not CompanyProfile.objects.filter(is_active=True).exists():
        CompanyProfile.objects.create(name="Corner Shop", is_active=True)
    return user


def features_post(client, *keys):
    data = {"action": "save_features"}
    data.update({f"module_{key}": "on" for key in keys})
    return client.post(reverse("settings_page"), data)


class RegistryTests(TestCase):
    def test_registry_is_consistent(self):
        keys = [module.key for module in modules.FEATURE_MODULES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertFalse(set(keys) & set(modules.CORE_MODULES))
        settings_fields = {field.name for field in SystemSettings._meta.get_fields()}
        for module in modules.FEATURE_MODULES:
            for flag in (*module.settings_flags, *module.dependent_flags):
                self.assertIn(flag, settings_fields, f"{module.key} maps onto a missing settings flag")
        for module_key in modules.REPORT_MODULES.values():
            self.assertIn(module_key, modules.MODULE_LOOKUP)

    def test_editions_only_name_real_modules_and_grow(self):
        basic = set(modules.edition_modules("basic"))
        standard = set(modules.edition_modules("standard"))
        self.assertTrue(basic < standard)
        self.assertTrue(standard < set(modules.MODULE_KEYS))
        self.assertIsNone(modules.edition_modules("enterprise"))
        with self.assertRaises(modules.ModuleError):
            modules.edition_modules("platinum")

    def test_core_modules_are_always_on_and_unknown_names_are_an_error(self):
        for key in modules.CORE_MODULES:
            self.assertTrue(modules.module_enabled(key))
        with self.assertRaises(modules.ModuleError):
            modules.module_enabled("teleport")


@override_settings(LICENSE_PUBLIC_KEY=TEST_PUBLIC_PEM.decode("ascii"))
class CeilingTests(TestCase):
    def test_trial_without_a_licence_means_everything(self):
        make_owner(trial_ended=False)

        state = modules.get_module_state()

        self.assertIsNone(state.ceiling)
        self.assertEqual(state.source, "open")
        for key in ("purchases", "accounts", "returns", "weights_units", "markup", "team", "backups"):
            self.assertTrue(modules.module_enabled(key), key)
            self.assertTrue(modules.module_within_ceiling(key), key)

    def test_old_licence_without_a_modules_field_means_everything(self):
        user = make_owner(trial_ended=True)
        key = issue_key()
        body = json.loads(licensing._b64decode(key.split(".")[0]))
        self.assertNotIn("modules", body)
        self.assertNotIn("edition", body)

        licence = licensing.activate_license(key, user=user)

        self.assertIsNone(licence.modules)
        self.assertIsNone(licensing.verify_license_key(key).modules)
        state = modules.get_module_state()
        self.assertEqual(state.source, "licence")
        self.assertIsNone(state.ceiling)
        self.assertTrue(modules.module_enabled("accounts"))
        self.assertEqual(licence.edition_label, "Enterprise")

    def test_licence_ceiling_is_respected(self):
        user = make_owner(trial_ended=True)
        licensing.activate_license(issue_key(edition="basic", modules=modules.edition_modules("basic")), user=user)

        state = modules.get_module_state()

        self.assertEqual(state.ceiling, frozenset(modules.edition_modules("basic")))
        self.assertEqual(state.edition, "basic")
        self.assertTrue(modules.module_enabled("reports"))
        self.assertFalse(modules.module_enabled("purchases"))
        self.assertFalse(modules.module_enabled("accounts"))
        self.assertFalse(modules.module_within_ceiling("accounts"))
        self.assertFalse(user_can_access_module(user, "purchases"))
        self.assertTrue(user_can_access_module(user, "products"))

    def test_licence_wins_over_a_running_trial(self):
        user = make_owner(trial_ended=False)
        licensing.activate_license(issue_key(modules=["reports"]), user=user)

        self.assertFalse(modules.module_enabled("purchases"))

    def test_ceiling_comes_from_the_signed_key_not_the_database_row(self):
        user = make_owner(trial_ended=True)
        licence = licensing.activate_license(issue_key(modules=["reports"]), user=user)
        License.objects.filter(pk=licence.pk).update(modules=None, edition="enterprise")

        self.assertFalse(modules.module_enabled("accounts"))

    def test_unknown_module_names_in_a_signed_key_are_ignored(self):
        user = make_owner(trial_ended=True)
        payload = licensing.build_payload(licensee="Corner Shop", days=30, modules=["reports"])
        payload["modules"] = ["reports", "hologram_till"]
        licensing.activate_license(licensing.sign_license(payload, TEST_PRIVATE_KEY), user=user)

        self.assertEqual(modules.get_module_state().ceiling, frozenset({"reports"}))

    def test_unreadable_module_list_is_rejected(self):
        payload = licensing.build_payload(licensee="Corner Shop", days=30)
        payload["modules"] = "everything"
        with self.assertRaisesMessage(licensing.LicenseError, "feature list"):
            licensing.verify_license_key(licensing.sign_license(payload, TEST_PRIVATE_KEY))

    def test_build_payload_refuses_unknown_modules(self):
        with self.assertRaisesMessage(licensing.LicenseError, "Unknown module"):
            licensing.build_payload(licensee="Corner Shop", days=30, modules=["reports", "nonsense"])

    def test_flags_of_modules_outside_the_edition_read_as_off_but_keep_the_stored_choice(self):
        user = make_owner(trial_ended=True)
        row = SystemSettings.load()
        row.gst_enabled = True
        row.enable_product_expiry = True
        row.save()
        licensing.activate_license(issue_key(modules=["reports", "price_control"]), user=user)

        masked = SystemSettings.load()
        self.assertFalse(masked.gst_enabled)
        self.assertFalse(masked.enable_product_expiry)
        self.assertFalse(masked.allow_credit_sales)  # belongs to Customers & Suppliers
        self.assertTrue(masked.allow_price_override)
        masked.tax_label = "VAT"
        masked.save()

        stored = SystemSettings.objects.get(pk=1)
        self.assertTrue(stored.gst_enabled)
        self.assertTrue(stored.enable_product_expiry)
        self.assertTrue(stored.allow_credit_sales)
        self.assertEqual(stored.tax_label, "VAT")
        self.assertFalse(masked.gst_enabled)

        # An upgrade gives the shop its own choices back.
        licensing.activate_license(issue_key(license_id="upgrade"), user=user)
        self.assertTrue(SystemSettings.load().gst_enabled)
        self.assertTrue(modules.module_enabled("tax"))

    def test_settings_form_cannot_turn_on_a_locked_flag(self):
        user = make_owner(trial_ended=True)
        licensing.activate_license(issue_key(modules=["reports"]), user=user)
        instance = SystemSettings.load()
        form = SystemSettingsForm(
            {
                "business-gst_enabled": "on",
                "business-enable_product_expiry": "on",
                "business-allow_price_override": "on",
                "business-tax_mode": SystemSettings.MANUAL_CART,
                "business-tax_label": "GST",
                "business-default_sale_payment_method": "cash",
                "business-thermal_printer_width_mm": "50",
            },
            prefix="business",
            instance=instance,
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        stored = SystemSettings.objects.get(pk=1)
        self.assertFalse(stored.gst_enabled)
        self.assertFalse(stored.enable_product_expiry)
        self.assertFalse(SystemSettings.load().allow_price_override)


@override_settings(LICENSE_PUBLIC_KEY=TEST_PUBLIC_PEM.decode("ascii"))
class OwnerSwitchTests(TestCase):
    def setUp(self):
        self.user = make_owner(trial_ended=True)
        self.client.force_login(self.user)

    def activate(self, **options):
        return licensing.activate_license(issue_key(**options), user=self.user)

    def test_owner_cannot_enable_a_locked_module_via_post(self):
        self.activate(edition="basic", modules=modules.edition_modules("basic"))

        response = features_post(self.client, "reports", "price_control", "hold_sale", "backups", "accounts", "purchases")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(modules.module_enabled("accounts"))
        self.assertFalse(modules.module_enabled("purchases"))
        self.assertEqual(SystemSettings.objects.get(pk=1).disabled_modules, [])
        follow = self.client.get(reverse("settings_page"))
        self.assertContains(follow, "Accounts is not included in your edition")
        self.assertEqual(self.client.get(reverse("accounts_home")).status_code, 302)

        with self.assertRaises(modules.ModuleLocked):
            modules.set_module_switch("accounts", True)

    def test_locked_flag_module_cannot_be_enabled_via_post(self):
        self.activate(modules=["reports"])

        features_post(self.client, "reports", "tax")

        self.assertFalse(SystemSettings.objects.get(pk=1).gst_enabled)
        self.assertFalse(modules.module_enabled("tax"))

    def test_features_section_lists_every_module_and_marks_locked_ones(self):
        self.activate(edition="basic", modules=modules.edition_modules("basic"))

        response = self.client.get(reverse("settings_page"))

        self.assertEqual(response.status_code, 200)
        rows = {row["key"]: row for group in response.context["feature_groups"] for row in group["rows"]}
        self.assertEqual(set(rows), set(modules.MODULE_KEYS))
        self.assertTrue(rows["accounts"]["locked"])
        self.assertFalse(rows["reports"]["locked"])
        self.assertContains(response, "Not included in your edition")
        self.assertContains(response, 'name="module_accounts"')
        self.assertEqual(response.context["edition_summary"]["label"], "Basic")

    def test_owner_switches_modules_off_and_on_inside_the_ceiling(self):
        self.activate()
        everything = set(modules.MODULE_KEYS)
        # Flag modules that are off by default stay off unless ticked.
        currently_on = {key for key in everything if modules.module_enabled(key)}

        features_post(self.client, *(currently_on - {"purchases", "returns"}))

        self.assertFalse(modules.module_enabled("purchases"))
        self.assertFalse(modules.module_enabled("returns"))
        self.assertTrue(modules.module_enabled("parties"))
        self.assertEqual(SystemSettings.objects.get(pk=1).disabled_modules, ["purchases", "returns"])

        features_post(self.client, *currently_on)

        self.assertTrue(modules.module_enabled("purchases"))
        self.assertEqual(SystemSettings.objects.get(pk=1).disabled_modules, [])

    def test_flag_modules_toggle_the_existing_setting_instead_of_a_second_switch(self):
        self.activate()
        currently_on = {key for key in modules.MODULE_KEYS if modules.module_enabled(key)}
        self.assertNotIn("tax", currently_on)

        features_post(self.client, *(currently_on | {"tax"}) - {"price_control"})

        stored = SystemSettings.objects.get(pk=1)
        self.assertTrue(stored.gst_enabled)
        self.assertFalse(stored.allow_price_override)
        self.assertFalse(stored.allow_line_discount)
        self.assertEqual(stored.disabled_modules, [])
        self.assertTrue(modules.module_enabled("tax"))
        self.assertFalse(modules.module_enabled("price_control"))

    def test_disabled_module_redirects_with_a_message_and_answers_ajax_with_403(self):
        self.activate()
        modules.set_module_switch("purchases", False)

        page = self.client.get(reverse("purchase_list"))
        ajax = self.client.get(reverse("purchase_list"), **AJAX)

        self.assertRedirects(page, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(ajax.status_code, 403)
        self.assertEqual(ajax.json()["module"], "purchases")
        self.assertIn("switched off", ajax.json()["message"])
        follow = self.client.get(reverse("dashboard"))
        self.assertContains(follow, "Purchases is switched off")

    def test_disabled_module_is_closed_to_admins_and_vanishes_from_navigation(self):
        self.activate()
        self.assertTrue(get_navigation_access(self.user)["accounts"])
        modules.set_module_switch("accounts", False)
        modules.set_module_switch("parties", False)

        access = get_navigation_access(self.user)
        response = self.client.get(reverse("dashboard"))

        self.assertFalse(access["accounts"])
        self.assertFalse(access["parties"])
        self.assertTrue(access["products"])
        self.assertFalse(response.context["nav_access"]["accounts"])
        self.assertFalse(response.context["feature_modules"]["accounts"])
        self.assertNotContains(response, f'href="{reverse("accounts_home")}"')
        self.assertNotContains(response, f'href="{reverse("parties_home")}"')
        self.assertContains(response, f'href="{reverse("product_list")}"')
        titles = [kpi["title"] for kpi in response.context["dashboard_kpis"]]
        self.assertNotIn("Customers Owe You", titles)
        self.assertIn("Today Net Sales", titles)
        # The API asks the same question.
        self.assertFalse(user_can_access_module(self.user, "parties"))

    def test_report_links_follow_the_modules(self):
        self.activate()
        slugs = {link["slug"] for group in report_navigation() for link in group["links"]}
        self.assertTrue({"returns", "sales-by-customer", "employees"} <= slugs)
        modules.set_module_switch("returns", False)
        modules.set_module_switch("parties", False)
        modules.set_module_switch("team", False)

        slugs = {link["slug"] for group in report_navigation() for link in group["links"]}
        home = self.client.get(reverse("reports_home"))
        returns_report = self.client.get(reverse("report_returns"))
        employees = self.client.get(reverse("employee_report_list"))

        self.assertFalse({"returns", "sales-by-customer", "employees"} & slugs)
        self.assertIn("sales-by-product", slugs)
        self.assertNotIn("Returns", [item["title"] for item in home.context["analysis_reports"]])
        self.assertEqual(returns_report.status_code, 302)
        self.assertEqual(employees.status_code, 302)

    def test_sub_features_are_refused_server_side(self):
        self.activate()
        for key in ("catalog_io", "hold_sale", "backups", "team"):
            modules.set_module_switch(key, False)

        export = self.client.get(reverse("product_export"))
        product_list = self.client.get(reverse("product_list"))
        hold = self.client.post(reverse("pos_screen"), {"action": "hold_cart"}, **AJAX)
        users_before = get_user_model().objects.count()
        new_user = {
            "action": "create_user",
            "team-username": "till2",
            "team-password1": "TillPass123!x",
            "team-password2": "TillPass123!x",
            "team-role": CASHIER_ROLE,
            "team-is_active": "on",
        }
        self.client.post(reverse("settings_page"), new_user)

        self.assertEqual(export.status_code, 302)
        self.assertNotContains(product_list, reverse("product_import"))
        self.assertEqual(hold.status_code, 403)
        self.assertEqual(get_user_model().objects.count(), users_before)

        # The very same requests work again once the modules are back on.
        for key in ("catalog_io", "team"):
            modules.set_module_switch(key, True)
        self.client.post(reverse("settings_page"), new_user)
        self.assertEqual(get_user_model().objects.count(), users_before + 1)
        self.assertEqual(self.client.get(reverse("product_export")).status_code, 200)
        self.assertContains(self.client.get(reverse("product_list")), reverse("product_import"))

    def test_returns_module_blocks_sale_and_purchase_returns(self):
        self.activate()
        modules.set_module_switch("returns", False)

        response = self.client.get(reverse("sale_return", args=[1]))

        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_unit_select_only_offers_whole_number_units_without_weights(self):
        self.activate()
        self.assertIn("kg", dict(ProductForm().fields["unit"].choices))
        modules.set_module_switch("weights_units", False)

        choices = dict(ProductForm().fields["unit"].choices)
        form = ProductForm(
            {"name": "Flour", "sku": "FL-1", "unit": "kg", "cost_price": "10", "price": "15", "minimum_margin_percent": "0", "low_stock_threshold": "1", "is_active": "on"}
        )
        flour = Product.objects.create(name="Old Flour", sku="FL-0", unit="kg", cost_price=Decimal("10"), price=Decimal("15"))

        # Box is not a counting unit while cartons and boxes are on: it is set under Packing.
        self.assertEqual(set(choices), {"pcs", "pack", "dozen"})
        self.assertFalse(form.is_valid())
        self.assertIn("unit", form.errors)
        # A product that is already sold by weight stays editable.
        self.assertIn("kg", dict(ProductForm(instance=flour).fields["unit"].choices))

    def test_markup_field_leaves_the_product_form_with_the_module(self):
        self.activate()
        self.assertIn("markup_percent", ProductForm().fields)
        modules.set_module_switch("markup", False)

        self.assertNotIn("markup_percent", ProductForm().fields)
        response = self.client.get(reverse("product_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Markup on cost")

    def test_core_screens_render_on_the_smallest_edition_and_with_everything_switched_off(self):
        self.activate(edition="basic", modules=modules.edition_modules("basic"))
        pages = ("dashboard", "pos_screen", "product_list", "product_add", "settings_page", "reports_home")
        for name in pages:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

        for key in modules.edition_modules("basic"):
            modules.set_module_switch(key, False)

        self.assertFalse(modules.get_module_state().enabled)
        for name in ("dashboard", "pos_screen", "product_list", "product_add", "settings_page"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        pos_page = self.client.get(reverse("pos_screen"))
        self.assertNotContains(pos_page, 'value="hold_cart"')
        self.assertNotContains(pos_page, 'id="is_credit"')
        self.assertEqual(self.client.get(reverse("reports_home")).status_code, 302)

    def test_cashier_is_also_refused(self):
        self.activate()
        cashier = get_user_model().objects.create_user(username="till", password="TillPass123!x", is_active=True)
        assign_role(cashier, CASHIER_ROLE)
        modules.set_module_switch("hold_sale", False)
        self.client.force_login(cashier)

        response = self.client.post(reverse("pos_screen"), {"action": "hold_cart"}, **AJAX)

        self.assertEqual(response.status_code, 403)

    def test_module_state_costs_no_query_per_question_inside_a_request(self):
        self.activate()
        token = modules.begin_request_scope()
        try:
            modules.module_enabled("purchases")
            with self.assertNumQueries(0):
                for key in modules.MODULE_KEYS:
                    modules.module_enabled(key)
                modules.enabled_module_map()
                get_navigation_access(self.user)
            # Saving the settings throws the cached answer away.
            modules.set_module_switch("purchases", False)
            self.assertFalse(modules.module_enabled("purchases"))
        finally:
            modules.end_request_scope(token)


class IssueCommandTests(TestCase):
    def issue(self, folder, **options):
        out = StringIO()
        call_command(
            "license_issue",
            licensee="Test Shop",
            days=30,
            private_key=str(Path(folder) / "private_key.pem"),
            stdout=out,
            stderr=StringIO(),
            **options,
        )
        return out.getvalue().strip()

    def test_issue_command_validates_module_names(self):
        with tempfile.TemporaryDirectory() as folder:
            call_command("license_keygen", dir=folder, stdout=StringIO())
            with self.assertRaisesMessage(CommandError, "Unknown module: warp_drive"):
                self.issue(folder, modules="purchases,warp_drive")
            with self.assertRaisesMessage(CommandError, "Unknown edition"):
                self.issue(folder, edition="platinum")
            with self.assertRaises(CommandError):
                self.issue(folder, modules=" , ")

    def test_issue_command_signs_edition_presets_and_custom_lists(self):
        with tempfile.TemporaryDirectory() as folder:
            call_command("license_keygen", dir=folder, stdout=StringIO())
            public_pem = (Path(folder) / "public_key.pem").read_text(encoding="ascii")
            with override_settings(LICENSE_PUBLIC_KEY=public_pem):
                standard = licensing.verify_license_key(self.issue(folder, edition="standard"))
                enterprise = licensing.verify_license_key(self.issue(folder, edition="enterprise"))
                custom = licensing.verify_license_key(self.issue(folder, edition="basic", modules="Returns, purchases"))
                plain = licensing.verify_license_key(self.issue(folder))

        self.assertEqual(standard.edition, "standard")
        self.assertEqual(standard.modules, modules.edition_modules("standard"))
        self.assertEqual(enterprise.edition, "enterprise")
        self.assertIsNone(enterprise.modules)
        self.assertEqual(custom.modules, ("purchases", "returns"))
        self.assertIsNone(plain.modules)
        self.assertEqual(plain.edition, "")
