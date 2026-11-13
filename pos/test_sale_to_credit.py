"""A paid sale can be put on credit afterwards, fully or in part, with the books following."""
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from accounts.models import AuditLog, VoucherLine
from accounts.services.party_balance import get_party_balance
from accounts.services.pos_posting import post_sale_from_pos
from pos.models import Customer, Product, Sale, SaleItem, SystemSettings
from pos.services.returns import create_sale_return
from pos.test_support import AuthenticatedAdminTestCase


def _lines(sale, title):
    lines = VoucherLine.objects.filter(voucher__ref_model="Sale", voucher__ref_id=sale.id, account__title=title)
    totals = lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))


class SaleToCreditTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(name="ali k/s")
        self.product = Product.objects.create(name="Albina", sku="ALB", cost_price=Decimal("600"), price=Decimal("950"), stock=10)

    def paid_sale(self, customer="default", quantity=5):
        customer = self.customer if customer == "default" else customer
        sale = Sale.objects.create(customer=customer, payment_method=Sale.CASH, sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.product, quantity=Decimal(quantity), price=Decimal("950"))
        sale.refresh_from_db()
        sale.paid_amount = sale.total_amount
        sale.save(update_fields=["paid_amount"])
        post_sale_from_pos(
            sale_id=sale.id,
            customer=customer,
            total_amount=sale.total_amount,
            paid_amount=sale.paid_amount,
            payment_method=sale.payment_method,
            sale_date=timezone.localdate(),
        )
        return sale

    def put_on_credit(self, sale, **data):
        data.setdefault("reason", "The shop did not pay")
        return self.client.post(reverse("sale_change_to_credit", args=[sale.id]), data)

    def test_the_whole_bill_goes_on_the_customers_account(self):
        sale = self.paid_sale()
        self.assertEqual(_lines(sale, "Cash"), Decimal("4750.00"))

        response = self.put_on_credit(sale, received="0")
        sale.refresh_from_db()

        self.assertRedirects(response, reverse("invoice", args=[sale.id]), fetch_redirect_response=False)
        self.assertTrue(sale.is_credit)
        self.assertEqual(sale.paid_amount, Decimal("0.00"))
        self.assertEqual(sale.balance_due, Decimal("4750.00"))
        self.assertEqual(_lines(sale, "Cash"), Decimal("0"))
        self.assertEqual(_lines(sale, "Accounts Receivable"), Decimal("4750.00"))
        self.assertEqual(get_party_balance(self.customer), Decimal("4750.00"))
        self.assertEqual(self.product.__class__.objects.get(pk=self.product.pk).stock, Decimal("5"))
        self.assertTrue(AuditLog.objects.filter(action="sale_changed_to_credit").exists())

    def test_part_of_the_bill_can_stay_paid(self):
        sale = self.paid_sale()

        self.put_on_credit(sale, received="1000")
        sale.refresh_from_db()

        self.assertEqual(sale.paid_amount, Decimal("1000.00"))
        self.assertEqual(_lines(sale, "Cash"), Decimal("1000.00"))
        self.assertEqual(get_party_balance(self.customer), Decimal("3750.00"))

    def test_a_walk_in_sale_needs_a_customer(self):
        sale = self.paid_sale(customer=None)

        self.put_on_credit(sale, received="0")
        sale.refresh_from_db()
        self.assertFalse(sale.is_credit)

        self.put_on_credit(sale, received="0", customer_id=self.customer.id)
        sale.refresh_from_db()
        self.assertTrue(sale.is_credit)
        self.assertEqual(sale.customer, self.customer)
        self.assertEqual(get_party_balance(self.customer), Decimal("4750.00"))

    def test_refused_without_a_reason_or_when_nothing_changes(self):
        sale = self.paid_sale()

        self.put_on_credit(sale, received="0", reason="")
        self.put_on_credit(sale, received="4750")
        sale.refresh_from_db()

        self.assertFalse(sale.is_credit)
        self.assertEqual(_lines(sale, "Cash"), Decimal("4750.00"))

    def test_the_credit_limit_still_applies(self):
        self.customer.credit_limit = Decimal("1000")
        self.customer.save()
        sale = self.paid_sale()

        self.put_on_credit(sale, received="0")
        sale.refresh_from_db()
        self.assertFalse(sale.is_credit)

        settings_row = SystemSettings.load()
        settings_row.credit_limit_action = SystemSettings.CREDIT_LIMIT_WARN
        settings_row.save()
        self.put_on_credit(sale, received="0")
        sale.refresh_from_db()
        self.assertTrue(sale.is_credit)

    def test_a_sale_with_returns_cannot_be_changed(self):
        sale = self.paid_sale()
        item = sale.items.get()
        create_sale_return(sale=sale, quantities={item.id: "1"}, refund_method="cash", reason="Broken", actor=self.admin_user)

        self.put_on_credit(sale, received="0")
        sale.refresh_from_db()

        self.assertFalse(sale.is_credit)

    def test_the_invoice_offers_it_only_while_something_was_paid(self):
        sale = self.paid_sale()
        self.assertContains(self.client.get(reverse("invoice", args=[sale.id])), "Not paid? Put on credit")

        self.put_on_credit(sale, received="0")
        page = self.client.get(reverse("invoice", args=[sale.id]))

        self.assertNotContains(page, "Not paid? Put on credit")
        self.assertContains(page, "On credit")
