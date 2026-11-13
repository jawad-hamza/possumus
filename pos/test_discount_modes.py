"""The bill discount typed as a percentage or as a cash amount.

Whichever is typed, the bill shows both, and a cash amount comes out exactly, to the paisa, on the
till, the saved sale, the invoice, the ledger posting, the reports and sale returns.
"""
import importlib
import random
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from django.apps import apps as django_apps
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Voucher
from pos.models import Category, Customer, Product, Sale, SaleItem, SystemSettings
from pos.services import discounts, reporting
from pos.services.business_rules import sale_discount_amount, sale_totals_from_rows
from pos.services.returns import create_sale_return
from pos.test_support import AuthenticatedAdminTestCase


AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
CENT = Decimal("0.01")

# (cash discount, subtotal): thirds, sevenths, one paisa, one paisa short of everything, the largest bill.
AWKWARD = [
    ("20.00", "215.00"),
    ("333.33", "9999.99"),
    ("0.01", "0.03"),
    ("1.00", "3.00"),
    ("0.01", "9999.99"),
    ("9999.98", "9999.99"),
    ("100.00", "300.01"),
    ("33.33", "100.00"),
    ("66.67", "100.01"),
    ("12345.67", "98765.43"),
    ("0.07", "0.49"),
    ("1234567.89", "7654321.01"),
    ("0.01", "9999999999.99"),
    ("3333333333.33", "9999999999.99"),
    ("9999999999.98", "9999999999.99"),
]


def flat(response):
    """The page as a reader sees it: every tag is a gap, spaces collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.content.decode("utf-8")))


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


class RateReproducesTheAmountTests(SimpleTestCase):
    """The one figure every calculation uses is the rate, so the rate must give the cash amount back."""

    def assert_exact(self, amount, subtotal):
        amount, subtotal = Decimal(amount), Decimal(subtotal)
        rate = discounts.rate_for_amount(amount, subtotal)
        self.assertEqual(rate, rate.quantize(discounts.RATE_PLACES), "the rate fits the ten decimals of the column")
        self.assertEqual(sale_discount_amount(subtotal, rate), amount, f"{amount} on {subtotal} through rate {rate}")
        self.assertEqual(money(subtotal - subtotal * rate / 100), subtotal - amount)

    def test_awkward_amounts_come_back_to_the_paisa(self):
        for amount, subtotal in AWKWARD:
            with self.subTest(amount=amount, subtotal=subtotal):
                self.assert_exact(amount, subtotal)

    def test_every_amount_on_every_small_subtotal(self):
        for subtotal_paisa in range(1, 251):
            for amount_paisa in range(1, subtotal_paisa + 1):
                self.assert_exact(Decimal(amount_paisa) / 100, Decimal(subtotal_paisa) / 100)

    def test_a_spread_of_subtotals_up_to_the_largest_bill(self):
        pick = random.Random(20261113)
        for _ in range(6000):
            subtotal_paisa = pick.randint(1, 10 ** pick.randint(2, 12) - 1)
            amount_paisa = pick.randint(1, subtotal_paisa)
            self.assert_exact(Decimal(amount_paisa) / 100, Decimal(subtotal_paisa) / 100)

    def test_a_typed_percentage_calculates_as_it_always_did(self):
        for rate, subtotal in [("10", "200"), ("12.5", "215"), ("33.33", "9999.99"), ("0.01", "3.33"), ("100", "45.67")]:
            with self.subTest(rate=rate, subtotal=subtotal):
                expected = money(Decimal(subtotal) * (money(rate) / 100))
                self.assertEqual(sale_discount_amount(Decimal(subtotal), rate), expected)

    def test_the_rate_stays_between_nothing_and_everything(self):
        self.assertEqual(discounts.rate_for_amount("0", "215"), Decimal("0"))
        self.assertEqual(discounts.rate_for_amount("-5", "215"), Decimal("0"))
        self.assertEqual(discounts.rate_for_amount("215", "215"), Decimal("100"))
        self.assertEqual(discounts.rate_for_amount("20", "0"), Decimal("0"))
        self.assertEqual(discounts.normalized_discount_rate("250"), Decimal("100"))
        self.assertEqual(discounts.normalized_discount_rate("-1"), Decimal("0"))
        self.assertEqual(discounts.normalized_discount_rate("nonsense"), Decimal("0"))
        self.assertEqual(discounts.normalized_discount_rate("NaN"), Decimal("0"))

    def test_percentages_print_with_up_to_two_decimals_and_no_trailing_zeros(self):
        cases = {"10": "10", "10.00": "10", "12.50": "12.5", "9.3023255814": "9.3", "12.345": "12.35", "3.3333": "3.33",
                 "100": "100", "0": "0", "0.004": "under 0.01"}
        for value, text in cases.items():
            with self.subTest(value=value):
                self.assertEqual(discounts.format_percent(value), text)

    def test_reading_what_was_typed(self):
        self.assertEqual(discounts.typed_discount({}), ("percent", "0"))
        self.assertEqual(discounts.typed_discount({"discount_rate": "10"}), ("percent", "10"))
        self.assertEqual(discounts.typed_discount({"discount_type": "amount", "discount_value": "20"}), ("amount", "20"))
        # The till's form posts the one box under its old name; the unit says what the figure is.
        self.assertEqual(discounts.typed_discount({"discount_type": "amount", "discount_rate": "20"}), ("amount", "20"))
        # The first source that carries a discount wins.
        self.assertEqual(
            discounts.typed_discount({"category": "3"}, {"discount_rate": "5"}, {"discount_type": "amount", "discount_value": "9"}),
            ("percent", "5"),
        )
        self.assertEqual(discounts.typed_discount({"discount_type": "bogus", "discount_rate": "7"}), ("percent", "7"))


class DiscountCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.ghee = Product.objects.create(name="Ghee", sku="GHEE", cost_price=Decimal("60.00"), price=Decimal("100.00"), stock=50)
        self.oil = Product.objects.create(name="Oil", sku="OIL", cost_price=Decimal("70.00"), price=Decimal("115.00"), stock=50)
        self.thirds = Product.objects.create(
            name="Thirds", sku="THIRDS", cost_price=Decimal("2000.00"), price=Decimal("3333.33"), stock=50
        )

    # -- the till ---------------------------------------------------------

    def till(self, action, data=None):
        return self.client.post(reverse("till_action", kwargs={"action": action}), data or {}, **AJAX)

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

    def checkout(self, **fields):
        payload = {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "cash"}
        payload.update(fields)
        return self.client.post(reverse("pos_screen"), payload)

    # -- sales made directly ----------------------------------------------

    def sale(self, lines, **fields):
        sale = Sale.objects.create(sold_by=self.admin_user, **fields)
        for line in lines:
            product, quantity = line[0], line[1]
            extra = line[2] if len(line) > 2 else {}
            SaleItem.objects.create(
                sale=sale, product=product, quantity=Decimal(str(quantity)), price=product.price,
                cost_price=product.cost_price, **extra
            )
        sale.refresh_from_db()
        sale.paid_amount = sale.total_amount
        sale.save(update_fields=["paid_amount"])
        return sale

    def cash_sale(self, lines, amount, **fields):
        return self.sale(lines, discount_type=Sale.DISCOUNT_AMOUNT, discount_value=Decimal(amount), **fields)

    def manual_tax(self):
        settings_obj = SystemSettings.load()
        settings_obj.gst_enabled = True
        settings_obj.tax_mode = SystemSettings.MANUAL_CART
        settings_obj.use_payment_method_gst = False
        settings_obj.save()

    def category_tax(self):
        settings_obj = SystemSettings.load()
        settings_obj.gst_enabled = True
        settings_obj.tax_mode = SystemSettings.CATEGORY_GST
        settings_obj.save()
        taxed = Category.objects.create(name="Taxed", tax_status=Category.TAXABLE, gst_rate=Decimal("18.00"))
        Product.objects.filter(pk=self.ghee.pk).update(category=taxed)
        self.ghee.refresh_from_db()


class WordingTests(DiscountCase):
    def test_typed_as_a_percentage_the_amount_follows(self):
        discount = discounts.resolve_bill_discount("percent", "10", "200")

        self.assertEqual(discount.amount, Decimal("20.00"))
        self.assertEqual(discount.label(), "Discount 10% = Rs 20.00")
        self.assertEqual(discount.parts(), ("Discount 10% =", "Rs 20.00"))
        self.assertEqual(discount.other(), "= Rs 20.00")
        self.assertEqual(discount.cell(), "10% = 20.00")

    def test_typed_as_cash_the_percentage_follows(self):
        discount = discounts.resolve_bill_discount("amount", "20", "215")

        self.assertEqual(discount.amount, Decimal("20.00"))
        self.assertEqual(discount.rate, Decimal("9.3023255814"))
        self.assertEqual(discount.label(), "Discount Rs 20.00 (9.3%)")
        self.assertEqual(discount.parts(), ("Discount", "Rs 20.00 (9.3%)"))
        self.assertEqual(discount.other(), "(9.3%)")
        self.assertEqual(discount.cell(), "20.00 (9.3%)")

    def test_the_currency_comes_from_settings(self):
        settings_obj = SystemSettings.load()
        settings_obj.currency_symbol = "AED"
        settings_obj.save()

        self.assertEqual(discounts.resolve_bill_discount("percent", "12.5", "80").label(), "Discount 12.5% = AED 10.00")
        self.assertEqual(discounts.resolve_bill_discount("amount", "10", "80").label(), "Discount AED 10.00 (12.5%)")
        self.assertEqual(discounts.discount_equals("12.50", "10"), "12.5% = AED 10.00")

    def test_a_cash_discount_is_refused_when_negative_or_more_than_the_bill(self):
        over = discounts.resolve_bill_discount("amount", "215.01", "215")
        negative = discounts.resolve_bill_discount("amount", "-5", "215")
        everything = discounts.resolve_bill_discount("amount", "215", "215")
        waiting = discounts.resolve_bill_discount("amount", "20", "0", has_lines=False)

        self.assertEqual(over.error, "Discount Rs 215.01 is more than the bill's subtotal of Rs 215.00.")
        self.assertEqual((over.rate, over.amount, over.value), (Decimal("0"), Decimal("0"), Decimal("215.01")))
        self.assertEqual(negative.error, "A discount cannot be less than zero.")
        self.assertEqual((negative.rate, negative.amount), (Decimal("0"), Decimal("0")))
        self.assertEqual((everything.error, everything.rate, everything.amount), ("", Decimal("100"), Decimal("215.00")))
        self.assertEqual((waiting.error, waiting.amount, waiting.value), ("", Decimal("0"), Decimal("20.00")))

    def test_a_typed_percentage_is_still_held_to_two_decimals_and_a_hundred(self):
        self.assertEqual(discounts.resolve_bill_discount("percent", "12.345", "100").rate, Decimal("12.35"))
        self.assertEqual(discounts.resolve_bill_discount("percent", "250", "100").amount, Decimal("100.00"))
        self.assertEqual(discounts.resolve_bill_discount("percent", "-3", "100").amount, Decimal("0.00"))


class SavedSaleTests(DiscountCase):
    def test_cash_discount_is_exact_on_awkward_bills(self):
        twenty = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00")
        thirds = self.cash_sale([(self.thirds, 3)], "333.33")

        self.assertEqual(twenty.subtotal, Decimal("215.00"))
        self.assertEqual(twenty.total_amount, Decimal("195.00"))
        self.assertEqual(twenty.discount_rate, Decimal("9.3023255814"))
        self.assertEqual(thirds.subtotal, Decimal("9999.99"))
        self.assertEqual(thirds.total_amount, Decimal("9666.66"))
        for sale, amount in ((twenty, "20.00"), (thirds, "333.33")):
            stored = Sale.objects.get(pk=sale.pk)
            self.assertEqual(stored.discount_type, Sale.DISCOUNT_AMOUNT)
            self.assertEqual(stored.discount_value, Decimal(amount))
            # The rate read back from the database gives the amount back: that is what the readers use.
            self.assertEqual(sale_discount_amount(stored.subtotal, stored.discount_rate), Decimal(amount))
            self.assertEqual(discounts.sale_bill_discount(stored).amount, Decimal(amount))

    def test_the_amount_stays_and_the_rate_moves_when_lines_change(self):
        sale = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00")
        extra = SaleItem.objects.create(sale=sale, product=self.ghee, quantity=1, price=Decimal("100.00"), cost_price=Decimal("60.00"))
        sale.refresh_from_db()

        self.assertEqual(sale.total_amount, Decimal("295.00"))
        self.assertEqual(sale.discount_value, Decimal("20.00"))
        self.assertEqual(sale.discount_rate, Decimal("6.3492063492"))

        extra.delete()
        sale.refresh_from_db()
        self.assertEqual(sale.total_amount, Decimal("195.00"))
        self.assertEqual(sale.discount_rate, Decimal("9.3023255814"))

    def test_tax_is_charged_on_the_exact_amount_left(self):
        # 215 - 20 = 195 exactly; at 0.5% the tax is 0.975, which is 0.98. A rate a hair too high would say 0.97.
        sale = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00", tax_rate=Decimal("0.50"))

        self.assertEqual(sale.tax_amount, Decimal("0.98"))
        self.assertEqual(sale.total_amount, Decimal("195.98"))

    def test_a_percentage_sale_calculates_exactly_as_before(self):
        for rate, tax in (("10", "0"), ("10", "10"), ("12.5", "5"), ("33.33", "17"), ("7.77", "0.5")):
            with self.subTest(rate=rate, tax=tax):
                sale = self.sale(
                    [(self.ghee, 1), (self.oil, 1), (self.thirds, 1)],
                    discount_rate=Decimal(rate), tax_rate=Decimal(tax),
                )
                subtotal = sale.subtotal
                after = subtotal - subtotal * (Decimal(rate) / 100)
                tax_amount = after * (Decimal(tax) / 100)

                self.assertEqual(sale.discount_type, Sale.DISCOUNT_PERCENT)
                self.assertEqual(sale.discount_rate, Decimal(rate))
                self.assertEqual(sale.tax_amount, money(tax_amount))
                self.assertEqual(sale.total_amount, money(after + tax_amount))

    def test_a_negative_discount_cannot_be_saved(self):
        from django.core.exceptions import ValidationError

        sale = Sale(discount_type=Sale.DISCOUNT_AMOUNT, discount_value=Decimal("-1.00"))
        with self.assertRaises(ValidationError):
            sale.full_clean()

    def test_existing_sales_are_recorded_as_typed_in_percent(self):
        old = self.sale([(self.ghee, 2)], discount_rate=Decimal("10.00"))
        plain = self.sale([(self.oil, 1)])
        Sale.objects.filter(pk__in=[old.pk, plain.pk]).update(discount_value=Decimal("0"))

        path = next(Path(__file__).parent.joinpath("migrations").glob("*_sale_discount_modes.py"))
        migration = importlib.import_module(f"pos.migrations.{path.stem}")
        migration.record_percent_discounts(django_apps, None)

        old.refresh_from_db()
        plain.refresh_from_db()
        self.assertEqual((old.discount_type, old.discount_value, old.discount_rate), ("percent", Decimal("10.00"), Decimal("10")))
        self.assertEqual(old.total_amount, Decimal("180.00"))
        self.assertEqual((plain.discount_type, plain.discount_value), ("percent", Decimal("0.00")))


class TillDiscountTests(DiscountCase):
    def test_a_percentage_shows_the_amount_beside_it(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        data = self.till("context", {"discount_rate": "10", "discount_type": "percent", "discount_value": "10"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["totals"]["discount"], "21.50")
        self.assertEqual(data["totals"]["total"], "193.50")
        self.assertEqual(data["discount"]["type"], "percent")
        self.assertEqual(data["discount"]["other"], "= Rs 21.50")
        self.assertEqual(data["discount"]["label"], "Discount 10% = Rs 21.50")
        self.assertEqual(data["context"]["discount_type"], "percent")
        self.assertEqual(Decimal(data["context"]["discount_value"]), Decimal("10"))

    def test_a_cash_amount_shows_the_percentage_beside_it(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        data = self.till("context", {"discount_type": "amount", "discount_value": "20", "discount_rate": "20"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["totals"]["discount"], "20.00")
        self.assertEqual(data["totals"]["total"], "195.00")
        self.assertEqual(data["discount"]["type"], "amount")
        self.assertEqual(data["discount"]["value"], "20.00")
        self.assertEqual(data["discount"]["other"], "(9.3%)")
        self.assertEqual(data["discount"]["label"], "Discount Rs 20.00 (9.3%)")
        self.assertEqual(data["context"]["discount_type"], "amount")
        self.assertEqual(data["context"]["discount_value"], "20.00")
        self.assertFalse(data["checkout_disabled"])

    def test_awkward_cash_amounts_are_exact_on_the_till(self):
        self.set_cart((self.thirds, 3))

        data = self.till("context", {"discount_type": "amount", "discount_value": "333.33"}).json()

        self.assertEqual(data["totals"]["subtotal"], "9999.99")
        self.assertEqual(data["totals"]["discount"], "333.33")
        self.assertEqual(data["totals"]["total"], "9666.66")
        self.assertEqual(data["discount"]["other"], "(3.33%)")

    def test_the_cash_amount_stays_as_typed_while_lines_come_and_go(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))
        self.till("context", {"discount_type": "amount", "discount_value": "20"})

        # The page posts the box and its unit with every cart change; a bare request keeps what is stored.
        more = self.till("add", {"product_id": self.ghee.id}).json()
        self.assertEqual(more["totals"]["subtotal"], "315.00")
        self.assertEqual(more["totals"]["discount"], "20.00")
        self.assertEqual(more["totals"]["total"], "295.00")
        self.assertEqual(more["discount"]["other"], "(6.35%)")
        self.assertEqual(more["context"]["discount_value"], "20.00")

        fewer = self.till("remove", {"product_id": self.oil.id, "discount_type": "amount", "discount_value": "20"}).json()
        self.assertEqual(fewer["totals"]["subtotal"], "200.00")
        self.assertEqual(fewer["totals"]["discount"], "20.00")
        self.assertEqual(fewer["discount"]["other"], "(10%)")

    def test_a_cash_amount_that_no_longer_fits_is_refused_not_trimmed(self):
        self.set_cart((self.ghee, 2), (self.oil, 1))
        self.till("context", {"discount_type": "amount", "discount_value": "250"})

        data = self.till("remove", {"product_id": self.ghee.id}).json()

        self.assertEqual(data["totals"]["subtotal"], "115.00")
        self.assertEqual(data["discount"]["error"], "Discount Rs 250.00 is more than the bill's subtotal of Rs 115.00.")
        self.assertEqual(data["notice"], {"kind": "out", "text": data["discount"]["error"]})
        self.assertTrue(data["checkout_disabled"])
        # Nothing is taken off while it does not fit, and the figure the cashier typed is still there.
        self.assertEqual(data["totals"]["discount"], "0.00")
        self.assertEqual(data["totals"]["total"], "115.00")
        self.assertEqual(data["context"]["discount_value"], "250.00")
        self.assertEqual(data["discount"]["other"], "")

        # Back on a bill it fits, it applies again without retyping.
        back = self.till("add", {"product_id": self.ghee.id, "quantity": "2"}).json()
        self.assertEqual(back["discount"]["error"], "")
        self.assertEqual(back["totals"]["total"], "65.00")
        self.assertFalse(back["checkout_disabled"])

    def test_typing_too_much_or_a_negative_amount_says_why(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        over = self.till("context", {"discount_type": "amount", "discount_value": "215.01"}).json()
        negative = self.till("context", {"discount_type": "amount", "discount_value": "-5"}).json()
        whole = self.till("context", {"discount_type": "amount", "discount_value": "215"}).json()

        self.assertFalse(over["ok"])
        self.assertEqual(over["status"], "invalid")
        self.assertIn("more than the bill's subtotal of Rs 215.00", over["notice"]["text"])
        self.assertTrue(over["checkout_disabled"])
        self.assertFalse(negative["ok"])
        self.assertEqual(negative["notice"]["text"], "A discount cannot be less than zero.")
        self.assertEqual(negative["totals"]["total"], "215.00")
        self.assertTrue(whole["ok"])
        self.assertEqual(whole["totals"]["total"], "0.00")

    def test_an_empty_bill_keeps_a_cash_amount_waiting_without_complaint(self):
        data = self.till("context", {"discount_type": "amount", "discount_value": "20"}).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["discount"]["error"], "")
        self.assertIsNone(data["notice"])

    def test_a_request_with_only_a_rate_still_means_a_percentage(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))
        self.till("context", {"discount_type": "amount", "discount_value": "20"})

        data = self.till("context", {"discount_rate": "10"}).json()

        self.assertEqual(data["context"]["discount_type"], "percent")
        self.assertEqual(data["totals"]["discount"], "21.50")

    def test_the_mode_survives_leaving_the_till_and_coming_back(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))
        self.till("context", {"discount_type": "amount", "discount_value": "20"})

        page = self.client.get(reverse("pos_screen"))

        discount = page.context["bill_discount"]
        self.assertEqual((discount.type, discount.value, discount.amount), ("amount", Decimal("20.00"), Decimal("20.00")))
        self.assertEqual(page.context["sale_preview"]["total_amount"], Decimal("195.00"))
        self.assertContains(page, 'name="discount_rate" id="discount_rate"')
        self.assertContains(page, 'value="20.00"')
        self.assertContains(page, 'id="discount_type_amount" aria-label="Cash amount" checked')
        self.assertContains(page, '<dd class="k-num" id="discount-amount">(9.3%)</dd>', html=True)
        self.assertContains(page, "zehnms/js/till-discount.js")

    def test_the_page_shows_a_percentage_as_it_always_did(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))
        self.till("context", {"discount_rate": "10"})

        page = self.client.get(reverse("pos_screen"))

        self.assertEqual(page.context["discount_rate_value"], Decimal("10.00"))
        self.assertContains(page, 'id="discount_type_percent" aria-label="Percentage" checked')
        self.assertContains(page, '<dd class="k-num" id="discount-amount">= Rs 21.50</dd>', html=True)
        self.assertNotContains(page, 'aria-invalid="true"')

    def test_the_page_keeps_the_reason_on_show_while_the_discount_does_not_fit(self):
        self.set_cart((self.oil, 1))
        self.till("context", {"discount_type": "amount", "discount_value": "250"})

        page = self.client.get(reverse("pos_screen"))

        self.assertTrue(page.context["checkout_disabled"])
        self.assertContains(page, "Discount Rs 250.00 is more than the bill&#x27;s subtotal of Rs 115.00.")
        self.assertContains(page, 'aria-invalid="true"')
        self.assertEqual(page.context["sale_preview"]["total_amount"], Decimal("115.00"))

    def test_a_held_sale_keeps_its_cash_discount(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        held = self.till("hold", {"discount_type": "amount", "discount_value": "20"}).json()
        self.assertEqual(held["context"]["discount_type"], "percent")
        self.assertEqual(Decimal(held["context"]["discount_value"]), Decimal("0"))

        resumed = self.till("resume", {"held_id": held["held"][0]["id"]}).json()
        self.assertEqual(resumed["context"]["discount_type"], "amount")
        self.assertEqual(resumed["context"]["discount_value"], "20.00")
        self.assertEqual(resumed["totals"]["discount"], "20.00")
        self.assertEqual(resumed["totals"]["total"], "195.00")
        self.assertEqual(resumed["discount"]["label"], "Discount Rs 20.00 (9.3%)")

    def test_holding_and_resuming_through_the_page_forms_keeps_it_too(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        hold = self.client.post(
            reverse("pos_screen"), {"action": "hold_cart", "discount_type": "amount", "discount_rate": "20"}
        )
        self.assertEqual(hold.status_code, 302)
        held_id = self.client.session["held_carts"][0]["id"]
        resume = self.client.post(reverse("pos_screen"), {"action": "resume_cart", "held_id": held_id})

        self.assertIn("discount_type=amount", resume.url)
        self.assertIn("discount_value=20.00", resume.url)
        page = self.client.get(resume.url)
        self.assertEqual(page.context["bill_discount"].amount, Decimal("20.00"))
        self.assertEqual(page.context["sale_preview"]["total_amount"], Decimal("195.00"))

    def test_a_line_discount_shows_what_it_comes_to(self):
        self.set_cart((self.ghee, 2))

        data = self.till("price", {"product_id": self.ghee.id, "custom_price": "100", "line_discount": "10"}).json()

        self.assertEqual(data["totals"]["line_discount"], "20.00")
        self.assertIn('<span class="t-disc-amt">= Rs 20.00</span>', data["html"]["bill"])
        plain = self.till("price", {"product_id": self.ghee.id, "custom_price": "100", "line_discount": "0"}).json()
        self.assertNotIn("t-disc-amt", plain["html"]["bill"])


class PreviewMatchesTheSaleTests(DiscountCase):
    """What the till shows before the sale closes is what the sale and the ledger then say."""

    def close_and_compare(self, **typed):
        preview = self.till("context", typed).json()["totals"]
        response = self.checkout(**typed)
        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.latest("id")
        self.assertEqual(str(sale.total_amount), preview["total"])
        self.assertEqual(str(sale.tax_amount), preview["tax"])
        return sale, preview

    def test_manual_tax(self):
        self.manual_tax()
        self.set_cart((self.ghee, 1), (self.oil, 1))

        sale, preview = self.close_and_compare(discount_type="amount", discount_value="20", tax_rate="0.5")

        self.assertEqual(preview["tax"], "0.98")
        self.assertEqual(sale.total_amount, Decimal("195.98"))

    def test_category_tax(self):
        self.category_tax()
        self.set_cart((self.ghee, 1), (self.oil, 1))

        sale, preview = self.close_and_compare(discount_type="amount", discount_value="33.33")

        # Ghee is 100 of the 215 and carries 18%: its share of the discount is 15.50, so 84.50 is taxed.
        self.assertEqual(preview["discount"], "33.33")
        self.assertEqual(sale.tax_mode, Sale.TAX_MODE_CATEGORY)
        self.assertEqual(sale.tax_amount, Decimal("15.21"))
        self.assertEqual(sale.total_amount, Decimal("196.88"))
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)
        self.assertTrue(voucher.lines.filter(account__title="Sales", credit=Decimal("181.67")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Sales Tax Payable", credit=Decimal("15.21")).exists())

    def test_no_tax(self):
        self.set_cart((self.thirds, 3))

        sale, preview = self.close_and_compare(discount_type="amount", discount_value="333.33")

        self.assertEqual(preview["total"], "9666.66")
        self.assertEqual(sale.discount_value, Decimal("333.33"))

    def test_preview_rows_take_the_rate_at_full_precision(self):
        rows = [{"line_total": Decimal("100.00")}, {"line_total": Decimal("115.00")}]
        rate = discounts.rate_for_amount("20", "215")

        preview = sale_totals_from_rows(rows, rate, 0, SystemSettings.load())

        self.assertEqual(preview["discount_amount"], Decimal("20.00"))
        self.assertEqual(preview["total_amount"], Decimal("195.00"))


class CheckoutTests(DiscountCase):
    def test_cash_discount_reaches_the_sale_and_the_ledger_exactly(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        # The page's form posts the box under its old name with the unit beside it.
        response = self.checkout(discount_type="amount", discount_rate="20")

        self.assertEqual(response.status_code, 302)
        sale = Sale.objects.get()
        self.assertEqual(sale.discount_type, Sale.DISCOUNT_AMOUNT)
        self.assertEqual(sale.discount_value, Decimal("20.00"))
        self.assertEqual(sale.discount_rate, Decimal("9.3023255814"))
        self.assertEqual(sale.total_amount, Decimal("195.00"))
        self.assertEqual(sale.paid_amount, Decimal("195.00"))
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)
        self.assertTrue(voucher.lines.filter(account__title="Sales", credit=Decimal("195.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Cash", debit=Decimal("195.00")).exists())
        self.assertNotIn("till_context", self.client.session)

    def test_a_third_of_a_bill_ending_in_nines(self):
        self.set_cart((self.thirds, 3))

        self.checkout(discount_type="amount", discount_value="333.33")

        sale = Sale.objects.get()
        self.assertEqual(sale.subtotal, Decimal("9999.99"))
        self.assertEqual(sale.total_amount, Decimal("9666.66"))
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)
        self.assertTrue(voucher.lines.filter(account__title="Sales", credit=Decimal("9666.66")).exists())

    def test_a_percentage_checkout_is_recorded_as_typed(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        self.checkout(discount_rate="10")

        sale = Sale.objects.get()
        self.assertEqual(sale.discount_type, Sale.DISCOUNT_PERCENT)
        self.assertEqual(sale.discount_rate, Decimal("10"))
        self.assertEqual(sale.discount_value, Decimal("10.00"))
        self.assertEqual(sale.total_amount, Decimal("193.50"))

    def test_checkout_refuses_a_cash_discount_larger_than_the_bill(self):
        self.set_cart((self.ghee, 1), (self.oil, 1))

        response = self.checkout(discount_type="amount", discount_value="500", respond="json")

        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Voucher.objects.filter(ref_model="Sale").count(), 0)
        self.ghee.refresh_from_db()
        self.assertEqual(self.ghee.stock, Decimal("50.000"))
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "refused")
        self.assertIn("Discount Rs 500.00 is more than the bill's subtotal of Rs 215.00.", data["notice"]["text"])
        self.assertEqual(len(self.client.session["cart"]), 2)

    def test_checkout_refuses_a_negative_cash_discount(self):
        self.set_cart((self.ghee, 1))

        response = self.checkout(discount_type="amount", discount_value="-10", respond="json")

        self.assertEqual(Sale.objects.count(), 0)
        self.assertIn("cannot be less than zero", response.json()["notice"]["text"])

    def test_a_cash_discount_equal_to_the_bill_follows_the_zero_total_rule(self):
        self.set_cart((self.ghee, 1))

        self.checkout(discount_type="amount", discount_value="100")
        self.assertEqual(Sale.objects.count(), 0)

        settings_obj = SystemSettings.load()
        settings_obj.allow_zero_total_sales = True
        settings_obj.save()
        self.checkout(discount_type="amount", discount_value="100")
        self.assertEqual(Sale.objects.get().total_amount, Decimal("0.00"))


class InvoiceAndReceiptTests(DiscountCase):
    def test_invoice_of_a_cash_discount(self):
        sale = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00")

        response = self.client.get(reverse("invoice", args=[sale.id]))

        self.assertEqual(response.context["discount_amount"], Decimal("20.00"))
        self.assertEqual(response.context["grand_total"], Decimal("195.00"))
        self.assertContains(response, "<span>Discount</span><span>Rs 20.00 (9.3%)</span>", count=2)
        self.assertIn("Discount Rs 20.00 (9.3%)", flat(response))
        self.assertNotContains(response, "Order discount")

    def test_invoice_of_a_percentage_discount(self):
        sale = self.sale([(self.ghee, 2)], discount_rate=Decimal("10.00"))

        response = self.client.get(reverse("invoice", args=[sale.id]))

        self.assertEqual(response.context["discount_amount"], Decimal("20.00"))
        self.assertContains(response, "<span>Discount 10% =</span><span>Rs 20.00</span>", count=2)
        self.assertIn("Discount 10% = Rs 20.00", flat(response))

    def test_invoice_of_the_awkward_third(self):
        sale = self.cash_sale([(self.thirds, 3)], "333.33")

        response = self.client.get(reverse("invoice", args=[sale.id]))

        self.assertIn("Discount Rs 333.33 (3.33%)", flat(response))
        self.assertIn("Total Rs 9,666.66", flat(response))

    def test_invoice_without_a_discount_has_no_discount_row(self):
        sale = self.sale([(self.ghee, 1)])

        response = self.client.get(reverse("invoice", args=[sale.id]))

        self.assertNotIn("Discount", flat(response).split("Subtotal", 1)[1])

    def test_a_line_discount_prints_its_amount_beside_the_percentage(self):
        sale = self.sale([(self.ghee, 2, {"discount_percent": Decimal("10.00")})])

        response = self.client.get(reverse("invoice", args=[sale.id]))

        # The discount sits in the line itself: "Disc 10% = Rs 20.00" on the receipt, a Disc column on A4.
        self.assertContains(response, "Disc 10% = Rs 20.00", count=1)
        self.assertContains(response, '<th scope="col" class="a-num">Disc</th>', html=False)
        self.assertNotContains(response, "Line discounts, included")

    def test_receipt_notes_and_the_discount_row(self):
        sale = self.cash_sale([(self.ghee, 2, {"discount_percent": Decimal("12.50")}), (self.oil, 1)], "20.00")
        item = sale.items.get(product=self.ghee)
        item.original_price = Decimal("110.00")

        def pkr(value):
            return f"PKR {value:,.2f}"

        self.assertEqual(discounts.receipt_item_notes(item, pkr), ["Custom PKR 100.00", "Disc 12.5% = PKR 25.00"])
        self.assertEqual(discounts.sale_bill_discount(sale).parts(pkr), ("Discount", "PKR 20.00 (6.9%)"))
        percent = self.sale([(self.ghee, 2)], discount_rate=Decimal("10.00"))
        self.assertEqual(discounts.sale_bill_discount(percent).parts(pkr), ("Discount 10% =", "PKR 20.00"))

    def test_receipt_pdf_is_drawn_for_both_kinds(self):
        cash = self.cash_sale([(self.ghee, 2, {"discount_percent": Decimal("12.50")}), (self.oil, 1)], "20.00")
        SaleItem.objects.filter(sale=cash, product=self.ghee).update(original_price=Decimal("110.00"))
        percent = self.sale([(self.ghee, 2)], discount_rate=Decimal("10.00"))

        for sale in (cash, percent):
            response = self.client.get(reverse("invoice_pdf", args=[sale.id]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Content-Type"], "application/pdf")
            self.assertTrue(response.content.startswith(b"%PDF"))


class ReportAndReturnTests(DiscountCase):
    def test_sales_by_invoice_shows_both_figures(self):
        cash = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00")
        percent = self.sale([(self.ghee, 2)], discount_rate=Decimal("10.00"))
        plain = self.sale([(self.oil, 1)])

        response = self.client.get(reverse("daily_sales_report"))

        rows = {row.id: row for row in response.context["sales"]}
        self.assertEqual(rows[cash.id].report_discount_amount, Decimal("20.00"))
        self.assertEqual(rows[cash.id].report_net_sales, Decimal("195.00"))
        self.assertEqual(rows[percent.id].report_discount_amount, Decimal("20.00"))
        self.assertContains(response, '<td class="k-num rp-extra" data-label="Discount">20.00 (9.3%)</td>', html=True)
        self.assertContains(response, '<td class="k-num rp-extra" data-label="Discount">10% = 20.00</td>', html=True)
        self.assertContains(response, '<td class="k-num rp-extra" data-label="Discount">0.00</td>', html=True)
        self.assertEqual(rows[plain.id].report_discount_amount, Decimal("0.00"))

    def test_sales_by_invoice_csv_carries_the_amount_and_the_percentage(self):
        import csv
        import io

        self.cash_sale([(self.thirds, 3)], "333.33")

        export = self.client.get(reverse("daily_sales_report"), {"export": "csv"})
        lines = list(csv.reader(io.StringIO(export.content.decode("utf-8-sig"))))

        header, line = lines[0], lines[1]
        self.assertEqual(line[header.index("Subtotal")], "9999.99")
        self.assertEqual(line[header.index("Discount")], "333.33")
        self.assertEqual(line[header.index("Discount %")], "3.33")
        self.assertEqual(line[header.index("Total")], "9666.66")

    def test_report_totals_and_sql_sums_are_exact(self):
        self.cash_sale([(self.thirds, 3)], "333.33")
        self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00")

        totals = reporting.sales_totals(self.today, self.today)
        by_product = reporting.build_sales_by_product(self.today, self.today, {})
        nets = {row["product"]: row["net_sales"] for row in by_product.rows}

        self.assertEqual(totals["net_sales"], Decimal("9861.66"))
        self.assertEqual(totals["total"], Decimal("9861.66"))
        # The database multiplies each line by (1 - rate%); the stored rate is fine enough to land on the paisa.
        self.assertEqual(nets["Thirds"], Decimal("9666.66"))
        self.assertEqual(nets["Ghee"], Decimal("90.70"))
        self.assertEqual(nets["Oil"], Decimal("104.30"))
        self.assertEqual(sum(nets.values()), Decimal("9861.66"))
        self.assertEqual(by_product.totals["net_sales"], Decimal("9861.66"))

    def test_a_partial_return_gives_back_the_share_that_was_paid(self):
        sale = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "20.00")
        ghee_line = sale.items.get(product=self.ghee)
        oil_line = sale.items.get(product=self.oil)

        first = create_sale_return(
            sale=sale, quantities={ghee_line.id: "1"}, refund_method="cash", reason="Changed mind", actor=self.admin_user
        )
        second = create_sale_return(
            sale=sale, quantities={oil_line.id: "1"}, refund_method="cash", reason="Changed mind", actor=self.admin_user
        )

        # 100 of the 215 carried 9.30 of the 20 discount, the 115 carried 10.70.
        self.assertEqual(first.net_amount, Decimal("90.70"))
        self.assertEqual(second.net_amount, Decimal("104.30"))
        self.assertEqual(first.total_amount + second.total_amount, sale.total_amount)

    def test_a_partial_return_of_a_percentage_sale_is_unchanged(self):
        sale = self.sale([(self.ghee, 4)], discount_rate=Decimal("10.00"), tax_rate=Decimal("10.00"))
        line = sale.items.get()

        returned = create_sale_return(
            sale=sale, quantities={line.id: "1"}, refund_method="cash", reason="Changed mind", actor=self.admin_user
        )

        self.assertEqual(returned.net_amount, Decimal("90.00"))
        self.assertEqual(returned.tax_amount, Decimal("9.00"))
        self.assertEqual(returned.total_amount, Decimal("99.00"))

    def test_a_customer_statement_carries_the_exact_credit_total(self):
        customer = Customer.objects.create(name="Bismillah Traders")
        self.set_cart((self.thirds, 3))

        self.checkout(discount_type="amount", discount_value="333.33", customer_id=customer.id, is_credit="on")

        sale = Sale.objects.get()
        self.assertTrue(sale.is_credit)
        self.assertEqual(sale.balance_due, Decimal("9666.66"))
        self.assertEqual(customer.balance, Decimal("9666.66"))


class ReturningEverythingTests(DiscountCase):
    """A cash discount is shared out to the paisa, so returning the whole sale refunds exactly what was paid."""

    def give_back(self, sale, item, quantity):
        return create_sale_return(
            sale=sale, quantities={item.id: str(quantity)}, refund_method="cash", reason="Changed mind",
            actor=self.admin_user,
        )

    def test_one_paisa_off_three_equal_lines(self):
        cheap = Product.objects.create(name="Matches", sku="MATCH", cost_price=Decimal("5.00"), price=Decimal("10.00"), stock=50)
        other = Product.objects.create(name="Candles", sku="CANDLE", cost_price=Decimal("5.00"), price=Decimal("10.00"), stock=50)
        third = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("5.00"), price=Decimal("10.00"), stock=50)
        sale = self.cash_sale([(cheap, 1), (other, 1), (third, 1)], "0.01")
        self.assertEqual(sale.total_amount, Decimal("29.99"))

        returns = [self.give_back(sale, item, 1) for item in sale.items.order_by("id")]

        # Each line alone at (1 - 0.0333...%) would round back to 10.00 and refund 30.00 for a 29.99 sale.
        self.assertEqual([r.net_amount for r in returns], [Decimal("10.00"), Decimal("10.00"), Decimal("9.99")])
        self.assertEqual(sum(r.total_amount for r in returns), Decimal("29.99"))

    def test_a_line_returned_one_piece_at_a_time(self):
        sale = self.cash_sale([(self.ghee, 7)], "1.00")
        line = sale.items.get()
        self.assertEqual(sale.total_amount, Decimal("699.00"))

        returns = [self.give_back(sale, line, 1) for _ in range(7)]

        self.assertEqual([r.net_amount for r in returns], [Decimal("99.86")] * 6 + [Decimal("99.84")])
        self.assertEqual(sum(r.total_amount for r in returns), sale.total_amount)

    def test_the_third_ending_in_nines_returned_in_pieces(self):
        sale = self.cash_sale([(self.thirds, 3)], "333.33")
        line = sale.items.get()

        first = self.give_back(sale, line, 2)
        rest = self.give_back(sale, line, 1)

        self.assertEqual(first.net_amount, Decimal("6444.44"))
        self.assertEqual(rest.net_amount, Decimal("3222.22"))
        self.assertEqual(first.total_amount + rest.total_amount, Decimal("9666.66"))

    def test_category_tax_given_back_matches_the_tax_charged(self):
        self.category_tax()
        sale = self.cash_sale([(self.ghee, 1), (self.oil, 1)], "33.33", tax_mode=Sale.TAX_MODE_CATEGORY)
        self.assertEqual(sale.tax_amount, Decimal("15.21"))

        returns = [self.give_back(sale, item, 1) for item in sale.items.order_by("id")]

        self.assertEqual([r.net_amount for r in returns], [Decimal("84.50"), Decimal("97.17")])
        self.assertEqual(sum(r.tax_amount for r in returns), sale.tax_amount)
        self.assertEqual(sum(r.total_amount for r in returns), sale.total_amount)

    def test_shares_add_up_to_the_net_on_awkward_bills(self):
        sale = self.cash_sale([(self.ghee, 3), (self.oil, 7), (self.thirds, 1)], "333.33")

        shares = discounts.line_shares(sale)

        self.assertEqual(sum(shares.values()), sale.subtotal - Decimal("333.33"))
        self.assertEqual(sum(shares.values()), sale.total_amount)
