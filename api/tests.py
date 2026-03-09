import json
from datetime import date
from decimal import Decimal

from django.urls import reverse

from accounts.models import Account, PartyLedgerEntry, Settlement, Voucher
from pos.models import Customer, Supplier
from pos.test_support import AuthenticatedAdminTestCase


class ApiFlowTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        accounts = [
            ("Cash", Account.ASSET),
            ("Bank", Account.ASSET),
            ("Accounts Receivable", Account.ASSET),
            ("Accounts Payable", Account.LIABILITY),
            ("Inventory", Account.ASSET),
            ("Sales", Account.INCOME),
            ("Cost of Goods Sold", Account.EXPENSE),
            ("Purchase", Account.EXPENSE),
        ]
        for title, acc_type in accounts:
            Account.objects.create(title=title, type=acc_type)

    def test_party_statement_api_returns_unified_contract(self):
        customer = Customer.objects.create(name="Unified Customer")
        supplier = Supplier.objects.create(name="Unified Supplier")

        response = self.client.get(
            reverse("party_statement_api"),
            {
                "customer_id": customer.id,
                "supplier_id": supplier.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["opening_balance"], "0.00")
        self.assertEqual(payload["closing_balance"], "0.00")
        self.assertIn("party", payload)
        self.assertEqual(payload["rows"], [])

    def test_unified_party_statement_keeps_opening_balance_with_filters(self):
        customer = Customer.objects.create(name="Filter Customer")
        supplier = Supplier.objects.create(name="Filter Supplier")

        prior_sale = Voucher.objects.create(
            voucher_type=Voucher.SALE,
            narration="Sale",
            ref_model="Sale",
            ref_id=1,
            date=date(2026, 3, 1),
        )
        PartyLedgerEntry.objects.create(voucher=prior_sale, party=customer, debit=Decimal("120.00"))

        prior_purchase = Voucher.objects.create(
            voucher_type=Voucher.PURCHASE,
            narration="Purchase",
            ref_model="Purchase",
            ref_id=2,
            date=date(2026, 3, 1),
        )
        PartyLedgerEntry.objects.create(voucher=prior_purchase, party=supplier, credit=Decimal("45.00"))

        in_range_receipt = Voucher.objects.create(
            voucher_type=Voucher.RECEIPT,
            narration="Receipt",
            ref_model="Settlement",
            ref_id=3,
            date=date(2026, 3, 2),
        )
        PartyLedgerEntry.objects.create(voucher=in_range_receipt, party=customer, credit=Decimal("20.00"))

        response = self.client.get(
            reverse("party_statement_api"),
            {
                "customer_id": customer.id,
                "supplier_id": supplier.id,
                "from": "2026-03-02",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["party"], "Filter Customer / Filter Supplier")
        self.assertEqual(payload["opening_balance"], "75.00")
        self.assertEqual(payload["closing_balance"], "55.00")
        self.assertEqual(len(payload["rows"]), 1)
    def test_supplier_statement_reports_payable_balance_type(self):
        supplier = Supplier.objects.create(name="Payable Supplier")
        voucher = Voucher.objects.create(voucher_type=Voucher.PURCHASE, narration="Purchase", ref_model="Purchase", ref_id=1)
        PartyLedgerEntry.objects.create(voucher=voucher, party=supplier, credit=Decimal("75.00"))

        response = self.client.get(reverse("party_statement_api"), {"supplier_id": supplier.id})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["balance_type"], "payable")
        self.assertEqual(payload["closing_balance"], "-75.00")

    def test_party_statement_rejects_invalid_dates(self):
        customer = Customer.objects.create(name="Date Customer")

        response = self.client.get(reverse("party_statement_api"), {"customer_id": customer.id, "from": "bad-date"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid date format. Use YYYY-MM-DD.")

    def test_settlement_api_creates_receipt_voucher(self):
        customer = Customer.objects.create(name="Receipt Customer")

        response = self.client.post(
            reverse("settlement_api"),
            data=json.dumps(
                {
                    "customer_id": customer.id,
                    "amount": "25.00",
                    "payment_method": "cash",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Settlement.objects.count(), 1)

        settlement = Settlement.objects.get()
        voucher = Voucher.objects.get(id=response.json()["voucher_id"])

        self.assertEqual(settlement.settlement_type, Settlement.RECEIPT)
        self.assertEqual(voucher.voucher_type, Voucher.RECEIPT)
        self.assertTrue(voucher.lines.filter(account__title="Cash", debit=Decimal("25.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Accounts Receivable", credit=Decimal("25.00")).exists())


