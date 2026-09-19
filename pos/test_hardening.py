from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Account, Voucher
from pos.models import Customer, Product, Purchase, PurchaseItem, Sale, SaleItem, StockAdjustment, Supplier
from pos.services.access import CASHIER_ROLE, assign_role
from pos.services.returns import create_purchase_return, create_sale_return
from pos.test_support import AuthenticatedAdminTestCase


PROTECTED_ROUTES = (
    ("scan_barcode", ["ANY-SKU"]),
    ("products_by_category", [1]),
    ("customer_add", []),
    ("supplier_add", []),
    ("purchase_return", [1]),
    ("invoice", [1]),
    ("invoice_pdf", [1]),
    ("dashboard", []),
    ("product_list", []),
)
PROTECTED_POST_ROUTES = ("quick_create_brand", "quick_create_category", "quick_create_supplier", "quick_create_product")


class AnonymousAccessTests(TestCase):
    def test_anonymous_visitors_are_sent_to_login(self):
        for name, args in PROTECTED_ROUTES:
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response.url)

    def test_anonymous_posts_cannot_create_records(self):
        for name in PROTECTED_POST_ROUTES:
            with self.subTest(route=name):
                response = self.client.post(reverse(name), {"quick_brand-name": "Injected"})
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response.url)

    def test_anonymous_ajax_gets_json_401(self):
        response = self.client.get(reverse("scan_barcode", args=["X"]), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 401)

    def test_public_pages_stay_open(self):
        for name in ("home", "login", "signup"):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class FreshInstallPostingTests(AuthenticatedAdminTestCase):
    """No chart of accounts is seeded here on purpose: a new shop must still be able to sell."""

    def test_first_checkout_creates_missing_core_accounts(self):
        product = Product.objects.create(name="Tea", sku="TEA-1", cost_price=Decimal("5.00"), price=Decimal("9.00"), stock=3)
        session = self.client.session
        session["cart"] = {
            str(product.id): {"name": "Tea", "price": "9.00", "original_price": "9.00", "discount_percent": "0.00", "quantity": "1"}
        }
        session.save()

        response = self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "payment_method": "cash", "discount_rate": "0", "tax_rate": "0"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertTrue(Account.objects.filter(title="Sales", type=Account.INCOME).exists())
        self.assertTrue(Account.objects.filter(title="Cost of Goods Sold", type=Account.EXPENSE).exists())


class WeightAndMarkupTests(AuthenticatedAdminTestCase):
    def test_weighed_product_sells_in_fractions(self):
        flour = Product.objects.create(
            name="Flour", sku="FLOUR", unit="kg", cost_price=Decimal("100.00"), price=Decimal("140.00"), stock=Decimal("10.000")
        )
        sale = Sale.objects.create(sold_by=self.admin_user, payment_method=Sale.CASH)

        item = SaleItem.objects.create(sale=sale, product=flour, quantity=Decimal("0.500"), price=Decimal("140.00"))

        flour.refresh_from_db()
        self.assertEqual(flour.stock, Decimal("9.500"))
        self.assertEqual(item.line_total, Decimal("70.00"))

    def test_piece_product_rejects_fractions(self):
        soap = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("10.00"), price=Decimal("15.00"), stock=5)
        self.assertTrue(soap.validate_quantity(Decimal("0.5")))
        self.assertEqual(soap.validate_quantity(Decimal("2")), "")

    def test_cart_accepts_typed_weight(self):
        flour = Product.objects.create(
            name="Flour", sku="FLOUR", unit="kg", cost_price=Decimal("100.00"), price=Decimal("140.00"), stock=Decimal("10.000")
        )
        response = self.client.post(
            reverse("pos_screen"), {"action": "set_quantity", "product_id": str(flour.id), "quantity": "0.25"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["cart"][str(flour.id)]["quantity"], "0.25")

    def test_markup_sets_sale_price_from_cost(self):
        response = self.client.post(
            reverse("product_add"),
            {
                "name": "Sugar",
                "sku": "SUGAR",
                "unit": "kg",
                "cost_price": "200.00",
                "markup_percent": "25",
                "price": "",
                "minimum_margin_percent": "10",
                "opening_stock": "12.5",
                "low_stock_threshold": "2",
                "is_active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        sugar = Product.objects.get(sku="SUGAR")
        self.assertEqual(sugar.price, Decimal("250.00"))
        self.assertEqual(sugar.stock, Decimal("12.500"))
        # Opening stock is equity, never income.
        adjustment = StockAdjustment.objects.get(product=sugar)
        self.assertEqual(adjustment.adjustment_mode, StockAdjustment.OPENING_STOCK)
        self.assertTrue(adjustment.voucher.lines.filter(account__title="Opening Balance Equity", credit=Decimal("2500.00")).exists())

    def test_purchase_reprices_markup_products(self):
        sugar = Product.objects.create(
            name="Sugar", sku="SUGAR", cost_price=Decimal("100.00"), price=Decimal("120.00"), markup_percent=Decimal("20.00")
        )
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)

        item = PurchaseItem(purchase=purchase, product=sugar, quantity=Decimal("10"), cost_price=Decimal("150.00"))
        item.save()

        sugar.refresh_from_db()
        self.assertEqual(item.sale_price, Decimal("180.00"))
        self.assertEqual(sugar.price, Decimal("180.00"))


class StockSafetyTests(AuthenticatedAdminTestCase):
    def test_product_edit_cannot_overwrite_stock(self):
        product = Product.objects.create(name="Pen", sku="PEN", cost_price=Decimal("5.00"), price=Decimal("9.00"), stock=10)
        Product.objects.filter(pk=product.pk).update(stock=7)  # a sale happened while the form was open

        response = self.client.post(
            reverse("product_edit", args=[product.id]),
            {
                "name": "Pen Blue",
                "sku": "PEN",
                "cost_price": "5.00",
                "price": "9.00",
                "minimum_margin_percent": "0",
                "stock": "10",
                "low_stock_threshold": "2",
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.name, "Pen Blue")
        self.assertEqual(product.stock, Decimal("7.000"))

    def test_same_product_twice_on_one_purchase_adds_both_lines(self):
        product = Product.objects.create(name="Pen", sku="PEN", cost_price=Decimal("5.00"), price=Decimal("9.00"), stock=10)
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)
        first = Product.objects.get(pk=product.pk)
        second = Product.objects.get(pk=product.pk)

        PurchaseItem(purchase=purchase, product=first, quantity=5, cost_price=Decimal("5.00"), sale_price=Decimal("9.00")).save()
        PurchaseItem(purchase=purchase, product=second, quantity=3, cost_price=Decimal("5.00"), sale_price=Decimal("9.00")).save()

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("18.000"))

    def test_resaving_old_purchase_line_keeps_current_price(self):
        product = Product.objects.create(name="Pen", sku="PEN", cost_price=Decimal("5.00"), price=Decimal("9.00"), stock=0)
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)
        item = PurchaseItem(purchase=purchase, product=product, quantity=5, cost_price=Decimal("5.00"), sale_price=Decimal("9.00"))
        item.save()
        Product.objects.filter(pk=product.pk).update(price=Decimal("12.00"))

        PurchaseItem.objects.get(pk=item.pk).save()

        product.refresh_from_db()
        self.assertEqual(product.price, Decimal("12.00"))


class ReturnFlowTests(AuthenticatedAdminTestCase):
    def _sale(self, *, is_credit=False, customer=None):
        product = Product.objects.create(name="Mug", sku="MUG", cost_price=Decimal("50.00"), price=Decimal("80.00"), stock=10)
        sale = Sale.objects.create(sold_by=self.admin_user, payment_method=Sale.CASH, is_credit=is_credit, customer=customer)
        item = SaleItem.objects.create(sale=sale, product=product, quantity=4, price=Decimal("80.00"))
        return sale, item, product

    def test_return_pages_render(self):
        sale, _, product = self._sale()
        purchase = Purchase.objects.create(payment_method=Purchase.CASH)
        PurchaseItem(purchase=purchase, product=product, quantity=2, cost_price=Decimal("50.00"), sale_price=Decimal("80.00")).save()

        self.assertEqual(self.client.get(reverse("sale_return", args=[sale.id])).status_code, 200)
        self.assertEqual(self.client.get(reverse("purchase_return", args=[purchase.id])).status_code, 200)

    def test_sale_return_restores_stock_and_balances(self):
        sale, item, product = self._sale()

        sale_return = create_sale_return(
            sale=sale, quantities={item.id: "1"}, refund_method="cash", reason="Damaged", actor=self.admin_user
        )

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("7.000"))
        voucher = Voucher.objects.get(ref_model="SaleReturn", ref_id=sale_return.id)
        lines = voucher.lines.all()
        self.assertEqual(sum(line.debit for line in lines), sum(line.credit for line in lines))

    def test_cannot_return_more_than_sold(self):
        sale, item, _ = self._sale()
        with self.assertRaises(ValueError):
            create_sale_return(sale=sale, quantities={item.id: "5"}, refund_method="cash", reason="Too many", actor=self.admin_user)

    def test_unpaid_credit_sale_cannot_be_refunded_in_cash(self):
        from accounts.services.pos_posting import post_sale_from_pos

        customer = Customer.objects.create(name="Ali")
        sale, item, _ = self._sale(is_credit=True, customer=customer)
        sale.refresh_from_db()
        post_sale_from_pos(
            sale_id=sale.id,
            customer=customer,
            total_amount=sale.total_amount,
            paid_amount=Decimal("0"),
            payment_method=Sale.CASH,
            sale_date=sale.created_at.date(),
            tax_amount=sale.tax_amount,
        )

        with self.assertRaises(ValueError):
            create_sale_return(sale=sale, quantities={item.id: "1"}, refund_method="cash", reason="Refund", actor=self.admin_user)

    def test_purchase_return_reduces_stock(self):
        supplier = Supplier.objects.create(name="ACME")
        product = Product.objects.create(name="Mug", sku="MUG2", cost_price=Decimal("50.00"), price=Decimal("80.00"), stock=0)
        purchase = Purchase.objects.create(payment_method=Purchase.CASH, supplier=supplier)
        item = PurchaseItem(purchase=purchase, product=product, quantity=6, cost_price=Decimal("50.00"), sale_price=Decimal("80.00"))
        item.save()

        create_purchase_return(
            purchase=purchase, quantities={item.id: "2"}, settlement_method="payable", reason="Faulty", actor=self.admin_user
        )

        product.refresh_from_db()
        self.assertEqual(product.stock, Decimal("4.000"))

    def test_cashier_cannot_return_someone_elses_sale(self):
        sale, _, _ = self._sale()
        cashier = get_user_model().objects.create_user(username="cashier", password="CashierPass123!")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)

        response = self.client.get(reverse("sale_return", args=[sale.id]))

        self.assertEqual(response.status_code, 302)


class ZeroTotalSaleTests(AuthenticatedAdminTestCase):
    def _checkout_with_full_discount(self):
        product = Product.objects.create(name="Sample", sku="SAMPLE", cost_price=Decimal("40.00"), price=Decimal("60.00"), stock=5)
        session = self.client.session
        session["cart"] = {
            str(product.id): {"name": "Sample", "price": "60.00", "original_price": "60.00", "discount_percent": "0.00", "quantity": "1"}
        }
        session.save()
        response = self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "payment_method": "cash", "discount_rate": "100", "tax_rate": "0"},
        )
        product.refresh_from_db()
        return response, product

    def test_refused_unless_the_owner_allows_it(self):
        _, product = self._checkout_with_full_discount()

        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(product.stock, Decimal("5.000"))

    def test_giveaway_moves_stock_and_posts_only_cost(self):
        from pos.models import SystemSettings

        settings_obj = SystemSettings.load()
        settings_obj.allow_zero_total_sales = True
        settings_obj.save()

        _, product = self._checkout_with_full_discount()

        sale = Sale.objects.get()
        self.assertEqual(sale.total_amount, Decimal("0.00"))
        self.assertEqual(product.stock, Decimal("4.000"))
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=sale.id)
        self.assertTrue(voucher.lines.filter(account__title="Cost of Goods Sold", debit=Decimal("40.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Inventory", credit=Decimal("40.00")).exists())
        self.assertFalse(voucher.lines.filter(account__title="Sales").exists())
