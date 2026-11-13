"""The purchases list reads like a supplier ledger: Debit (paid), Credit (bill) and a running Balance."""
import re
from decimal import Decimal

from django.urls import reverse

from accounts.services.settlements import create_payment
from pos.models import Purchase, Supplier
from pos.services.purchase_balances import outstanding_purchase_total, running_purchase_balances
from pos.test_support import AuthenticatedAdminTestCase


class PurchaseListLedgerTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.khan = Supplier.objects.create(name="khan trader")
        self.hameed = Supplier.objects.create(name="hameed")
        self.first = Purchase.objects.create(supplier=self.khan, total_amount=Decimal("33200"), paid_amount=Decimal("18000"))
        self.second = Purchase.objects.create(supplier=self.hameed, total_amount=Decimal("31400"), paid_amount=Decimal("31400"))

    def test_the_balance_runs_from_the_oldest_bill_and_ends_on_what_is_owed(self):
        balances = running_purchase_balances()

        self.assertEqual(balances[self.first.id], Decimal("15200.00"))
        self.assertEqual(balances[self.second.id], Decimal("15200.00"))
        self.assertEqual(balances[self.second.id], outstanding_purchase_total())

    def test_a_later_supplier_payment_lowers_the_balance(self):
        create_payment(party=self.khan, amount=Decimal("5000"), payment_method="cash")

        balances = running_purchase_balances()

        self.assertEqual(balances[self.first.id], Decimal("10200.00"))
        self.assertEqual(balances[self.second.id], outstanding_purchase_total())

    def test_the_list_shows_debit_credit_and_balance(self):
        page = self.client.get(reverse("purchase_list")).content.decode()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))

        for heading in ("Debit", "Credit", "Balance"):
            self.assertIn(f'data-label="{heading}"', page)
        self.assertRegex(text, r"Rs 18,000\.00 Rs 33,200\.00 Rs 15,200\.00 Part paid Rs 15,200\.00 due")
        self.assertRegex(text, r"Rs 31,400\.00 Rs 31,400\.00 Rs 15,200\.00 Paid")
