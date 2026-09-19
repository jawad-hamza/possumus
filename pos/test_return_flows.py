"""Adding or editing something returns the user exactly where he started.

Covers the validated ?next= on every create/edit/delete page, the list address carried into
row links, and the side-panel quick-create endpoints (permission and validation).
"""
from decimal import Decimal
from urllib.parse import quote

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils.html import escape

from accounts.models import BankAccount, ExpenseCategory
from pos.models import Brand, Category, Product, Purchase, Supplier
from pos.services.access import CASHIER_ROLE, assign_role
from pos.services.navigation import get_safe_next_url, safe_next_url, with_query, with_touched_row
from pos.test_support import AuthenticatedAdminTestCase

PRODUCT_POST = {
    "name": "Rice",
    "sku": "RICE-1",
    "unit": "pcs",
    "cost_price": "100.00",
    "price_by": "manual",
    "price": "140.00",
    "minimum_margin_percent": "10",
    "low_stock_threshold": "5",
    "is_active": "on",
}

LIST_URL = "/products/?q=ri&brand=&sort=price_high&page=2"
UNSAFE_NEXTS = [
    "https://evil.example/",
    "//evil.example/steal",
    "/\\evil.example",
    "javascript:alert(1)",
    "http://testserver.evil.example/",
]


def assert_never_offered(test, response, target):
    """The unsafe address is not used as any link, back target or hidden next field on the page."""
    test.assertNotContains(response, f'="{escape(target)}')


class SafeNextHelperTests(TestCase):
    def _request(self, **params):
        return RequestFactory().get("/products/", params)

    def test_accepts_a_same_site_path_with_its_query(self):
        self.assertEqual(safe_next_url(self._request(next=LIST_URL)), LIST_URL)

    def test_rejects_other_hosts_and_schemes(self):
        for target in UNSAFE_NEXTS:
            with self.subTest(target=target):
                self.assertEqual(safe_next_url(self._request(next=target)), "")
                self.assertEqual(get_safe_next_url(self._request(next=target), "product_list"), reverse("product_list"))

    def test_with_query_and_touched_row(self):
        self.assertEqual(with_query("/purchases/add/?next=%2Fpurchases%2F", supplier_id=7), "/purchases/add/?next=%2Fpurchases%2F&supplier_id=7")
        self.assertEqual(with_query("/products/add/?copy=3", copy=None, next="/products/"), "/products/add/?next=%2Fproducts%2F")
        self.assertEqual(with_touched_row(LIST_URL, 9), LIST_URL + "#touched-9")
        self.assertEqual(with_touched_row("/products/#top", 9), "/products/#top")


class ProductReturnFlowTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.product = Product.objects.create(name="Sugar", sku="SUG-1", cost_price=Decimal("80"), price=Decimal("100"), stock=4)

    def test_list_rows_carry_the_list_address_they_came_from(self):
        response = self.client.get(reverse("product_list"), {"q": "sug", "sort": "price_high"})

        here = quote("/products/?q=sug&sort=price_high", safe="/")
        self.assertContains(response, f'{reverse("product_edit", args=[self.product.id])}?next={here}')
        self.assertContains(response, f'{reverse("product_view", args=[self.product.id])}?next={here}')
        self.assertContains(response, f'{reverse("product_delete", args=[self.product.id])}?next={here}')
        self.assertContains(response, f'data-rf-row="{self.product.id}"')
        self.assertContains(response, "zehnms/js/return-flow.js")

    def test_edit_returns_to_the_filtered_sorted_page_with_the_row_marked(self):
        edit_page = self.client.get(reverse("product_edit", args=[self.product.id]), {"next": LIST_URL})
        self.assertContains(edit_page, f'name="next" value="{LIST_URL.replace("&", "&amp;")}"')
        self.assertContains(edit_page, f'href="{LIST_URL.replace("&", "&amp;")}"')

        response = self.client.post(
            reverse("product_edit", args=[self.product.id]),
            {**PRODUCT_POST, "name": "Sugar", "sku": "SUG-1", "next": LIST_URL},
        )

        self.assertRedirects(response, f"{LIST_URL}#touched-{self.product.id}", fetch_redirect_response=False)

    def test_edit_without_next_still_opens_the_product(self):
        response = self.client.post(reverse("product_edit", args=[self.product.id]), {**PRODUCT_POST, "name": "Sugar", "sku": "SUG-1"})
        self.assertRedirects(response, reverse("product_view", args=[self.product.id]), fetch_redirect_response=False)

    def test_unsafe_next_is_ignored_on_every_product_page(self):
        for target in UNSAFE_NEXTS:
            with self.subTest(target=target):
                Product.objects.filter(sku="RICE-1").delete()
                added = self.client.post(reverse("product_add"), {**PRODUCT_POST, "next": target})
                self.assertRedirects(added, reverse("product_view", args=[Product.objects.get(sku="RICE-1").id]), fetch_redirect_response=False)
                edited = self.client.post(reverse("product_edit", args=[self.product.id]), {**PRODUCT_POST, "name": "Sugar", "sku": "SUG-1", "next": target})
                self.assertRedirects(edited, reverse("product_view", args=[self.product.id]), fetch_redirect_response=False)
                page = self.client.get(reverse("product_edit", args=[self.product.id]), {"next": target})
                assert_never_offered(self, page, target)

    def test_add_returns_to_the_list_and_save_and_add_another_keeps_it(self):
        response = self.client.post(reverse("product_add"), {**PRODUCT_POST, "next": LIST_URL})
        rice = Product.objects.get(sku="RICE-1")
        self.assertRedirects(response, f"{LIST_URL}#touched-{rice.id}", fetch_redirect_response=False)

        again = self.client.post(reverse("product_add"), {**PRODUCT_POST, "sku": "RICE-2", "save_and_new": "1", "next": LIST_URL})
        self.assertRedirects(again, f"{reverse('product_add')}?{'next=' + quote(LIST_URL, safe='')}", fetch_redirect_response=False)
        blank = self.client.get(again["Location"])
        self.assertContains(blank, f'name="next" value="{LIST_URL.replace("&", "&amp;")}"')

    def test_another_size_keeps_the_list_too(self):
        response = self.client.post(
            f"{reverse('product_add')}?copy={self.product.id}",
            {**PRODUCT_POST, "name": "Sugar", "sku": "SUG-2", "variant_label": "Large", "source_variant_label": "Small", "save_and_new": "1", "next": LIST_URL},
        )
        new_size = Product.objects.get(sku="SUG-2")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"copy={new_size.id}", response["Location"])
        self.assertIn("next=" + quote(LIST_URL, safe=""), response["Location"])

    def test_product_view_links_back_to_the_list_it_was_opened_from(self):
        response = self.client.get(reverse("product_view", args=[self.product.id]), {"next": LIST_URL})
        self.assertContains(response, f'href="{LIST_URL.replace("&", "&amp;")}"')
        # Edit from here comes back to this view, which still knows the list.
        view_here = quote(f"{reverse('product_view', args=[self.product.id])}?next={quote(LIST_URL, safe='')}", safe="/")
        self.assertContains(response, f"{reverse('product_edit', args=[self.product.id])}?next={view_here}")

    def test_delete_returns_to_the_list_but_never_to_the_deleted_product(self):
        response = self.client.post(reverse("product_delete", args=[self.product.id]), {"next": LIST_URL})
        self.assertRedirects(response, LIST_URL, fetch_redirect_response=False)

        other = Product.objects.create(name="Salt", sku="SALT-1", cost_price=Decimal("10"), price=Decimal("20"))
        own_page = reverse("product_view", args=[other.id])
        response = self.client.post(reverse("product_delete", args=[other.id]), {"next": own_page})
        self.assertRedirects(response, reverse("product_list"), fetch_redirect_response=False)

    def test_till_add_link_prefills_the_scanned_barcode(self):
        response = self.client.get(reverse("product_add"), {"barcode": "8901234", "next": reverse("pos_screen")})
        self.assertEqual(response.context["form"]["barcode"].value(), "8901234")
        self.assertEqual(response.context["back_url"], reverse("pos_screen"))


class BrandCategoryReturnTests(AuthenticatedAdminTestCase):
    def test_brand_add_honours_next_and_defaults_to_products(self):
        response = self.client.post(reverse("brand_add"), {"name": "Sunrise", "is_active": "on", "next": LIST_URL})
        self.assertRedirects(response, LIST_URL, fetch_redirect_response=False)
        response = self.client.post(reverse("brand_add"), {"name": "Moonrise", "is_active": "on", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("product_list"), fetch_redirect_response=False)
        page = self.client.get(reverse("brand_add"), {"next": LIST_URL})
        self.assertContains(page, 'name="next"')

    def test_category_add_and_edit_honour_next(self):
        response = self.client.post(reverse("category_add"), {"name": "Grains", "tax_status": Category.EXEMPT if hasattr(Category, "EXEMPT") else "exempt", "gst_rate": "0", "is_active": "on", "next": LIST_URL})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], LIST_URL)
        category = Category.objects.get(name="Grains")
        response = self.client.post(
            reverse("category_edit", args=[category.id]),
            {"name": "Grain", "tax_status": category.tax_status, "gst_rate": "0", "is_active": "on", "next": "//evil.example/"},
        )
        self.assertRedirects(response, reverse("settings_page"), fetch_redirect_response=False)


class PurchaseReturnFlowTests(AuthenticatedAdminTestCase):
    def _payload(self, product, **extra):
        payload = {
            "payment_method": "cash",
            "paid_amount": "30.00",
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
            "items-0-product": str(product.id),
            "items-0-quantity": "3",
            "items-0-cost_price": "10.00",
            "items-0-sale_price": "15.00",
        }
        payload.update(extra)
        return payload

    def setUp(self):
        super().setUp()
        self.product = Product.objects.create(name="Rice", sku="RICE-9", cost_price=Decimal("10"), price=Decimal("15"))

    def test_purchase_add_and_edit_honour_next(self):
        list_url = f"{reverse('purchase_list')}?page=2"
        response = self.client.post(reverse("purchase_add"), self._payload(self.product, next=list_url))
        purchase = Purchase.objects.get()
        self.assertRedirects(response, f"{list_url}#touched-{purchase.id}", fetch_redirect_response=False)

        item = purchase.items.get()
        response = self.client.post(
            reverse("purchase_edit", args=[purchase.id]),
            self._payload(self.product, reason="Typo", next="https://evil.example/", **{"items-INITIAL_FORMS": "1", "items-0-id": str(item.id)}),
        )
        self.assertRedirects(response, reverse("purchase_detail", args=[purchase.id]), fetch_redirect_response=False)

    def test_purchase_list_rows_carry_the_list_address(self):
        response = self.client.post(reverse("purchase_add"), self._payload(self.product))
        purchase = Purchase.objects.get()
        self.assertRedirects(response, reverse("purchase_detail", args=[purchase.id]), fetch_redirect_response=False)
        listing = self.client.get(reverse("purchase_list"))
        self.assertContains(listing, f'{reverse("purchase_edit", args=[purchase.id])}?next={quote(reverse("purchase_list"), safe="/")}')
        self.assertContains(listing, f'data-rf-row="{purchase.id}"')

    def test_new_supplier_comes_back_selected_on_the_purchase(self):
        purchase_url = f"{reverse('purchase_add')}?next={quote(reverse('purchase_list'), safe='')}"
        response = self.client.post(
            reverse("supplier_add"),
            {"name": "ACME Traders", "phone": "", "email": "", "address": "", "next": purchase_url},
        )
        supplier = Supplier.objects.get(name="ACME Traders")
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"supplier_id={supplier.id}", response["Location"])
        page = self.client.get(response["Location"])
        self.assertEqual(page.context["purchase_form"]["supplier"].value(), supplier.id)
        # The purchase form still knows the list it will return to.
        self.assertEqual(page.context["next_url"], reverse("purchase_list"))


class StockAdjustmentReturnTests(AuthenticatedAdminTestCase):
    def test_validated_next_only(self):
        product = Product.objects.create(name="Tea", sku="TEA-1", cost_price=Decimal("5"), price=Decimal("8"), stock=2)
        page = self.client.get(reverse("stock_adjustments"), {"product": product.id, "next": "https://evil.example/"})
        assert_never_offered(self, page, "https://evil.example/")
        self.assertNotContains(page, 'name="next"')
        page = self.client.get(reverse("stock_adjustments"), {"product": product.id, "next": LIST_URL})
        self.assertContains(page, 'name="next"')


class QuickCreateEndpointTests(AuthenticatedAdminTestCase):
    def test_brand_category_supplier_are_created_and_returned(self):
        brand = self.client.post(reverse("quick_create_brand"), {"quick_brand-name": "Sunrise"}).json()
        category = self.client.post(reverse("quick_create_category"), {"quick_category-name": "Grains"}).json()
        supplier = self.client.post(reverse("quick_create_supplier"), {"quick_supplier-name": "ACME"}).json()

        self.assertEqual(brand["brand"]["id"], Brand.objects.get(name="Sunrise").id)
        self.assertEqual(category["category"]["id"], Category.objects.get(name="Grains").id)
        self.assertEqual(supplier["supplier"]["id"], Supplier.objects.get(name="ACME").id)

    def test_validation_errors_come_back_as_json(self):
        response = self.client.post(reverse("quick_create_brand"), {"quick_brand-name": ""})
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.json()["errors"])
        response = self.client.post(reverse("quick_create_bank_account"), {"quick_bank-name": "Meezan"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("account_number", response.json()["errors"])

    def test_get_is_refused(self):
        self.assertEqual(self.client.get(reverse("quick_create_brand")).status_code, 405)
        self.assertEqual(self.client.get(reverse("quick_create_expense_type")).status_code, 405)

    def test_expense_type_and_bank_account(self):
        response = self.client.post(
            reverse("quick_create_expense_type"),
            {"quick_expense_type-name": "Internet", "quick_expense_type-expense_pattern": ExpenseCategory.CUSTOM},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["expense_type"]["name"], "Internet")
        # Asking again hands back the same one instead of an error.
        again = self.client.post(
            reverse("quick_create_expense_type"),
            {"quick_expense_type-name": "internet", "quick_expense_type-expense_pattern": ExpenseCategory.CUSTOM},
        )
        self.assertEqual(again.json()["expense_type"]["id"], response.json()["expense_type"]["id"])

        bank = self.client.post(
            reverse("quick_create_bank_account"),
            {"quick_bank-name": "Meezan", "quick_bank-account_number": "0101", "quick_bank-is_active": "on"},
        )
        self.assertEqual(bank.status_code, 200, bank.content)
        self.assertEqual(bank.json()["bank"]["id"], BankAccount.objects.get(account_number="0101").id)

    def test_pages_offer_the_side_panel(self):
        product_form = self.client.get(reverse("product_add"))
        self.assertContains(product_form, 'data-rf-quick="brand"')
        self.assertContains(product_form, 'data-rf-quick-form="supplier"')
        self.assertContains(product_form, reverse("quick_create_category"))
        expenses = self.client.get(reverse("expense_list"))
        self.assertContains(expenses, 'data-rf-quick-form="expense_type"')
        self.assertContains(expenses, 'data-rf-quick-form="bank"')


class QuickCreatePermissionTests(TestCase):
    def setUp(self):
        self.cashier = get_user_model().objects.create_user(username="till", password="TillPass123!")
        assign_role(self.cashier, CASHIER_ROLE)
        self.client.force_login(self.cashier)

    def test_a_cashier_cannot_create_supporting_records(self):
        posts = [
            ("quick_create_brand", {"quick_brand-name": "Sneaky"}),
            ("quick_create_category", {"quick_category-name": "Sneaky"}),
            ("quick_create_supplier", {"quick_supplier-name": "Sneaky"}),
            ("quick_create_expense_type", {"quick_expense_type-name": "Sneaky", "quick_expense_type-expense_pattern": ExpenseCategory.CUSTOM}),
            ("quick_create_bank_account", {"quick_bank-name": "Sneaky", "quick_bank-account_number": "1"}),
        ]
        for url_name, data in posts:
            with self.subTest(url_name=url_name):
                response = self.client.post(reverse(url_name), data)
                self.assertNotEqual(response.status_code, 200)
        self.assertFalse(Brand.objects.filter(name="Sneaky").exists())
        self.assertFalse(Category.objects.filter(name="Sneaky").exists())
        self.assertFalse(Supplier.objects.filter(name="Sneaky").exists())
        self.assertFalse(ExpenseCategory.objects.filter(name="Sneaky").exists())
        self.assertFalse(BankAccount.objects.filter(name="Sneaky").exists())
