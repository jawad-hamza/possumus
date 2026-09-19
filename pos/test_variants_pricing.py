"""Markup on cost (a percentage or a fixed amount) and items sold in several sizes."""
import csv
import io
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from pos.forms import ProductForm, QuickProductForm
from pos.models import Product, ProductGroup, Purchase, PurchaseItem, Sale, SaleItem
from pos.services import modules
from pos.services.catalog_io import COLUMNS, apply_catalog, export_catalog_csv_text, parse_catalog
from pos.services.reporting import best_selling_products
from pos.test_support import AuthenticatedAdminTestCase


def _product(sku, **overrides):
    values = {
        "name": f"Product {sku}",
        "sku": sku,
        "cost_price": Decimal("100.00"),
        "price": Decimal("125.00"),
        "stock": Decimal("10"),
    }
    values.update(overrides)
    return Product.objects.create(**values)


def _bread():
    group = ProductGroup.objects.create(name="Bread")
    sizes = [
        _product(sku, name="Bread", variant_group=group, variant_label=label, cost_price=Decimal("50.00"), price=Decimal(price), barcode=barcode)
        for label, sku, price, barcode in (
            ("Small", "BREAD-S", "80.00", "900001"),
            ("Medium", "BREAD-M", "120.00", "900002"),
            ("Large", "BREAD-L", "160.00", "900003"),
        )
    ]
    return group, sizes


PRODUCT_POST = {
    "name": "Rice",
    "sku": "RICE-1",
    "unit": "pcs",
    "cost_price": "100.00",
    "minimum_margin_percent": "10",
    "low_stock_threshold": "5",
    "is_active": "on",
}


class MarkupOnCostTests(AuthenticatedAdminTestCase):
    def test_price_for_cost_follows_a_percentage_or_a_fixed_amount(self):
        by_percent = Product(cost_price=Decimal("100"), markup_percent=Decimal("25"))
        by_amount = Product(cost_price=Decimal("100"), markup_amount=Decimal("20"))
        by_hand = Product(cost_price=Decimal("100"), price=Decimal("150"))

        self.assertEqual(by_percent.price_for_cost(), Decimal("125.00"))
        self.assertEqual(by_amount.price_for_cost(), Decimal("120.00"))
        self.assertEqual(by_amount.price_for_cost(Decimal("80")), Decimal("100.00"))
        self.assertIsNone(by_hand.price_for_cost())
        self.assertEqual(by_amount.markup_display, "Cost + 20.00")

    def test_both_markups_at_once_are_refused(self):
        product = Product(name="X", sku="X", cost_price=Decimal("100"), price=Decimal("0"), markup_percent=Decimal("10"), markup_amount=Decimal("5"))

        with self.assertRaises(ValidationError) as caught:
            product.full_clean()

        self.assertIn("markup_amount", caught.exception.message_dict)

    def test_amount_below_the_minimum_margin_is_refused_with_the_lowest_amount(self):
        product = Product(
            name="X", sku="X", cost_price=Decimal("100"), price=Decimal("0"),
            markup_amount=Decimal("5"), minimum_margin_percent=Decimal("10"),
        )

        with self.assertRaises(ValidationError) as caught:
            product.full_clean()

        self.assertIn("Add at least 10.00", caught.exception.message_dict["markup_amount"][0])

    def test_product_form_prices_by_amount_on_cost(self):
        response = self.client.post(reverse("product_add"), {**PRODUCT_POST, "price_by": "amount", "markup_amount": "20", "markup_percent": "30"})

        self.assertEqual(response.status_code, 302)
        product = Product.objects.get(sku="RICE-1")
        self.assertEqual(product.price, Decimal("120.00"))
        self.assertEqual(product.markup_amount, Decimal("20.00"))
        # Only the chosen way is kept: the percentage typed earlier is dropped.
        self.assertIsNone(product.markup_percent)

    def test_product_form_prices_by_percentage_and_by_hand(self):
        self.client.post(reverse("product_add"), {**PRODUCT_POST, "price_by": "percent", "markup_percent": "25", "markup_amount": "20"})
        self.client.post(reverse("product_add"), {**PRODUCT_POST, "sku": "RICE-2", "price_by": "manual", "price": "140", "markup_percent": "25"})

        by_percent = Product.objects.get(sku="RICE-1")
        by_hand = Product.objects.get(sku="RICE-2")
        self.assertEqual((by_percent.price, by_percent.markup_percent, by_percent.markup_amount), (Decimal("125.00"), Decimal("25.00"), None))
        self.assertEqual((by_hand.price, by_hand.markup_percent, by_hand.markup_amount), (Decimal("140.00"), None, None))

    def test_amount_that_breaks_the_minimum_margin_shows_a_clear_message(self):
        response = self.client.post(reverse("product_add"), {**PRODUCT_POST, "price_by": "amount", "markup_amount": "5"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add at least")
        self.assertFalse(Product.objects.filter(sku="RICE-1").exists())

    def test_price_by_offers_only_typing_when_the_markup_module_is_off(self):
        modules.set_module_switch("markup", False)

        form = ProductForm()

        self.assertNotIn("price_by", form.fields)
        self.assertNotIn("markup_amount", form.fields)
        self.assertNotIn("markup_percent", form.fields)

    def test_purchase_reprices_from_the_new_cost_with_a_fixed_amount(self):
        product = _product("OIL-1", markup_amount=Decimal("20.00"), cost_price=Decimal("100.00"), price=Decimal("120.00"), stock=Decimal("0"))
        purchase = Purchase.objects.create()

        PurchaseItem.objects.create(purchase=purchase, product=product, quantity=Decimal("5"), cost_price=Decimal("150.00"))

        product.refresh_from_db()
        self.assertEqual(product.cost_price, Decimal("150.00"))
        self.assertEqual(product.price, Decimal("170.00"))

    def test_purchase_reprices_from_the_new_cost_with_a_percentage(self):
        product = _product("OIL-2", markup_percent=Decimal("10.00"), cost_price=Decimal("100.00"), price=Decimal("110.00"), stock=Decimal("0"))
        purchase = Purchase.objects.create()

        PurchaseItem.objects.create(purchase=purchase, product=product, quantity=Decimal("2"), cost_price=Decimal("200.00"))

        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("220.00"))

    def test_purchase_line_guards_the_minimum_margin_on_a_fixed_amount(self):
        product = _product("OIL-3", markup_amount=Decimal("5.00"), minimum_margin_percent=Decimal("10"), cost_price=Decimal("10.00"), price=Decimal("15.00"))
        item = PurchaseItem(purchase=Purchase.objects.create(), product=product, quantity=Decimal("1"), cost_price=Decimal("100.00"))

        with self.assertRaises(ValidationError) as caught:
            item.clean()

        self.assertIn("Cost + 5.00 on this cost gives 105.00", caught.exception.message_dict["sale_price"][0])

    def test_purchase_select_options_carry_the_fixed_amount(self):
        _product("OIL-4", markup_amount=Decimal("20.00"))

        response = self.client.get(reverse("purchase_add"))

        self.assertContains(response, 'data-markup-amount="20.00"')

    def test_quick_product_accepts_an_amount_on_cost_without_a_price(self):
        form = QuickProductForm(data={"name": "Ghee", "sku": "GHEE-1", "unit": "pcs", "markup_amount": "30", "minimum_margin_percent": "0", "low_stock_threshold": "5"})

        self.assertTrue(form.is_valid(), form.errors)
        product = form.save()
        self.assertEqual(product.markup_amount, Decimal("30.00"))


class VariantModelTests(AuthenticatedAdminTestCase):
    def test_display_name_names_the_size(self):
        _group, (small, _medium, _large) = _bread()
        plain = _product("TEA")

        self.assertEqual(small.display_name, "Bread - Small")
        self.assertEqual(str(small), "Bread - Small")
        self.assertEqual(plain.display_name, "Product TEA")

    def test_two_sizes_of_one_item_cannot_share_a_label(self):
        group, _sizes = _bread()
        duplicate = Product(name="Bread", sku="BREAD-X", price=Decimal("90"), variant_group=group, variant_label="small")

        with self.assertRaises(ValidationError) as caught:
            duplicate.full_clean()
        self.assertIn("variant_label", caught.exception.message_dict)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.create(name="Bread", sku="BREAD-Y", price=Decimal("90"), variant_group=group, variant_label="Small")

    def test_a_size_needs_a_label(self):
        group, _sizes = _bread()
        unnamed = Product(name="Bread", sku="BREAD-Z", price=Decimal("90"), variant_group=group)

        with self.assertRaises(ValidationError) as caught:
            unnamed.full_clean()
        self.assertIn("variant_label", caught.exception.message_dict)

    def test_renaming_the_family_renames_every_size(self):
        group, sizes = _bread()

        group.name = "Naan"
        group.save()

        self.assertEqual(set(Product.objects.filter(variant_group=group).values_list("name", flat=True)), {"Naan"})
        sizes[0].refresh_from_db()
        self.assertEqual(sizes[0].display_name, "Naan - Small")


class VariantScreensTests(AuthenticatedAdminTestCase):
    def test_add_a_size_to_a_plain_product_makes_a_family(self):
        source = _product("CAKE", name="Cake", price=Decimal("300"))

        form_page = self.client.get(reverse("product_add"), {"copy": source.id})
        self.assertContains(form_page, "Sizes and variants")
        self.assertContains(form_page, 'name="source_variant_label"')

        self.client.post(
            f"{reverse('product_add')}?copy={source.id}",
            {**PRODUCT_POST, "name": "Cake", "sku": "CAKE-2", "price": "500", "minimum_margin_percent": "0",
             "variant_label": "1 kg", "source_variant_label": "Half kg"},
        )

        source.refresh_from_db()
        second = Product.objects.get(sku="CAKE-2")
        self.assertEqual(source.variant_group_id, second.variant_group_id)
        self.assertEqual(source.display_name, "Cake - Half kg")
        self.assertEqual(second.display_name, "Cake - 1 kg")

    def test_the_two_first_sizes_need_different_names(self):
        source = _product("CAKE", name="Cake", price=Decimal("300"))

        response = self.client.post(
            f"{reverse('product_add')}?copy={source.id}",
            {**PRODUCT_POST, "name": "Cake", "sku": "CAKE-2", "price": "500", "minimum_margin_percent": "0",
             "variant_label": "Small", "source_variant_label": "small"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Give the two sizes different names.")
        self.assertIsNone(Product.objects.get(sku="CAKE").variant_group_id)

    def test_a_size_added_to_a_family_joins_it(self):
        group, (small, _medium, _large) = _bread()

        self.client.post(
            f"{reverse('product_add')}?copy={small.id}",
            {**PRODUCT_POST, "name": "Bread", "sku": "BREAD-XL", "price": "200", "minimum_margin_percent": "0", "variant_label": "Family pack"},
        )

        self.assertEqual(Product.objects.get(sku="BREAD-XL").variant_group_id, group.id)
        self.assertEqual(group.variants.count(), 4)

    def test_edit_form_lists_the_sibling_sizes(self):
        _group, (small, medium, _large) = _bread()

        response = self.client.get(reverse("product_edit", args=[small.id]))

        self.assertContains(response, "Sizes and variants")
        self.assertContains(response, reverse("product_edit", args=[medium.id]))
        self.assertContains(response, "BREAD-M")

    def test_list_folds_the_sizes_into_one_family_row(self):
        _bread()
        _product("TEA", name="Tea")

        response = self.client.get(reverse("product_list"))

        rows = response.context["product_rows"]
        families = [row for row in rows if row["is_family"]]
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(families), 1)
        family = families[0]
        self.assertEqual(family["size_count"], 3)
        self.assertEqual((family["price_low"], family["price_high"]), (Decimal("80.00"), Decimal("160.00")))
        self.assertEqual(family["stock_total"], Decimal("30"))

    def test_list_search_finds_sizes_by_label_sku_and_barcode(self):
        _bread()
        _product("TEA", name="Tea")

        for term in ("medium", "BREAD-L", "900001", "bread small"):
            rows = self.client.get(reverse("product_list"), {"q": term}).context["product_rows"]
            self.assertEqual(len(rows), 1, term)
            self.assertTrue(rows[0]["is_family"], term)

    def test_list_filters_still_apply_inside_a_family(self):
        _group, (small, _medium, _large) = _bread()
        Product.objects.filter(pk=small.pk).update(stock=Decimal("1"))

        rows = self.client.get(reverse("product_list"), {"status": "low_stock"}).context["product_rows"]

        self.assertEqual(len(rows), 1)
        self.assertEqual([variant.pk for variant in rows[0]["variants"]], [small.pk])

    def test_module_off_shows_every_size_as_its_own_product(self):
        _bread()
        modules.set_module_switch("variants", False)

        rows = self.client.get(reverse("product_list")).context["product_rows"]
        catalogue = self.client.get(reverse("till_catalogue")).json()
        small = Product.objects.get(sku="BREAD-S")
        edit = self.client.get(reverse("product_edit", args=[small.id]))

        self.assertEqual(len(rows), 3)
        self.assertFalse(any(row["is_family"] for row in rows))
        self.assertEqual(catalogue["families"], [])
        self.assertTrue(all(row["family"] == 0 for row in catalogue["products"]))
        self.assertIn("Bread - Small", {row["name"] for row in catalogue["products"]})
        self.assertNotContains(edit, "Sizes and variants")
        self.assertNotContains(edit, 'name="variant_label"')


class VariantTillTests(AuthenticatedAdminTestCase):
    def test_catalogue_offers_the_family_once_with_its_sizes(self):
        group, sizes = _bread()

        data = self.client.get(reverse("till_catalogue")).json()

        self.assertEqual(data["families"], [{"id": group.id, "name": "Bread", "sizes": [size.id for size in sizes]}])
        rows = {row["id"]: row for row in data["products"]}
        self.assertEqual(rows[sizes[0].id]["family"], group.id)
        self.assertEqual(rows[sizes[0].id]["label"], "Small")
        self.assertEqual(rows[sizes[0].id]["name"], "Bread - Small")
        self.assertIn("small", rows[sizes[0].id]["find"])

    def test_scanning_a_barcode_adds_that_exact_size(self):
        _group, (_small, medium, _large) = _bread()

        data = self.client.post(reverse("till_action", args=["scan"]), {"code": "900002"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(list(self.client.session["cart"].keys()), [str(medium.id)])

    def test_an_exact_sku_with_a_quantity_adds_that_size(self):
        _group, (_small, _medium, large) = _bread()

        self.client.post(reverse("till_action", args=["scan"]), {"code": "BREAD-L", "quantity": "2"})

        self.assertEqual(Decimal(self.client.session["cart"][str(large.id)]["quantity"]), Decimal("2"))

    def test_a_picked_size_sells_under_its_full_name_on_the_receipt(self):
        _group, (small, _medium, _large) = _bread()
        self.client.post(reverse("till_action", args=["add"]), {"product_id": small.id, "quantity": "2"})

        payload = self.client.post(reverse("till_action", args=["context"]), {}).json()
        self.assertIn("Bread - Small", str(payload))

        self.client.post(reverse("pos_screen"), {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"})
        sale = Sale.objects.get()
        item = SaleItem.objects.get(sale=sale)
        self.assertEqual((item.product_id, item.price), (small.id, Decimal("80.00")))
        self.assertContains(self.client.get(reverse("invoice", args=[sale.id])), "Bread - Small")
        self.assertEqual(best_selling_products()[0]["product__name"], "Bread - Small")


class VariantCsvTests(AuthenticatedAdminTestCase):
    def test_export_carries_the_family_the_size_and_the_amount_on_cost(self):
        _bread()
        _product("OIL", name="Oil", markup_amount=Decimal("20.00"), price=Decimal("120.00"))

        rows = list(csv.DictReader(io.StringIO(export_catalog_csv_text().lstrip("﻿"))))

        by_sku = {row["sku"]: row for row in rows}
        self.assertIn("variant_group", COLUMNS)
        self.assertEqual((by_sku["BREAD-S"]["variant_group"], by_sku["BREAD-S"]["variant_label"]), ("Bread", "Small"))
        self.assertEqual(by_sku["OIL"]["variant_group"], "")
        self.assertEqual(by_sku["OIL"]["markup_amount"], "20.00")

    def test_round_trip_rebuilds_the_family_on_a_fresh_catalog(self):
        _bread()
        _product("OIL", name="Oil", markup_amount=Decimal("20.00"), price=Decimal("120.00"))
        text = export_catalog_csv_text()
        Product.objects.all().delete()
        ProductGroup.objects.all().delete()

        plan = parse_catalog(text.encode("utf-8"))
        self.assertTrue(plan.is_valid, plan.errors)
        apply_catalog(plan)

        group = ProductGroup.objects.get()
        self.assertEqual(group.name, "Bread")
        self.assertEqual(
            sorted(Product.objects.filter(variant_group=group).values_list("variant_label", "price")),
            [("Large", Decimal("160.00")), ("Medium", Decimal("120.00")), ("Small", Decimal("80.00"))],
        )
        oil = Product.objects.get(sku="OIL")
        self.assertEqual((oil.markup_amount, oil.markup_percent), (Decimal("20.00"), None))

    def test_import_refuses_a_size_twice_and_a_size_without_a_family(self):
        text = (
            "name,sku,price,variant_group,variant_label\n"
            "Bread,B1,80,Bread,Small\n"
            "Bread,B2,90,Bread,small\n"
            "Tea,T1,50,,Large\n"
        )

        plan = parse_catalog(text.encode("utf-8"))

        messages = " ".join(message for error in plan.errors for message in error["messages"])
        self.assertIn("is also on row 2", messages)
        self.assertIn("fill variant_group too", messages)

    def test_import_with_both_markups_is_refused(self):
        plan = parse_catalog(b"name,sku,cost_price,markup_percent,markup_amount\nOil,O1,100,10,20\n")

        self.assertFalse(plan.is_valid)
