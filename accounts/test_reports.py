import csv
import datetime
import io
from decimal import Decimal

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from accounts.models import (
    Account,
    AccountingSettings,
    Expense,
    ExpenseCategory,
    PartyLedgerEntry,
    Settlement,
    Voucher,
    VoucherLine,
)
from accounts.services.aging import PAYABLE, RECEIVABLE, get_aging
from accounts.services.balance_sheet import get_balance_sheet
from accounts.services.books_lock import set_books_lock
from accounts.services.cash_flow import get_cash_flow
from accounts.services.chart_of_accounts import ensure_default_accounts
from accounts.services.current_actor import acting_as
from accounts.services.day_book import get_day_book
from accounts.services.expense_by_category import get_expense_by_category
from accounts.services.expense_posting import post_expense
from accounts.services.ledgers import get_account_ledger, get_party_ledger, ledger_rows
from accounts.services.party_balance import get_party_balance
from accounts.services.party_opening import THEY_OWE_US, WE_OWE_THEM, get_party_opening, set_party_opening_balance
from accounts.services.pos_posting import post_purchase_from_pos, post_sale_from_pos
from accounts.services.profit_loss import get_profit_and_loss
from accounts.services.settlements import create_payment, create_receipt
from accounts.services.trial_balance import get_trial_balance
from pos.models import Customer, Product, Purchase, PurchaseItem, Sale, SaleItem, Supplier
from pos.services.returns import create_purchase_return, create_sale_return
from pos.test_support import AuthenticatedAdminTestCase


def _csv_rows(response):
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


class BooksFixtureMixin:
    """A small but complete set of books: purchase, sale, return, expense, settlements."""

    @classmethod
    def build_books(cls):
        ensure_default_accounts()
        cls.customer = Customer.objects.create(name="Ali Traders", phone="0300")
        cls.supplier = Supplier.objects.create(name="Flour Mills")
        cls.product = Product.objects.create(
            name="Flour", sku="FLOUR", unit="kg", cost_price=Decimal("50.00"), price=Decimal("80.00"), stock=0
        )

        # Purchase 20 kg on credit, 400 paid in cash.
        cls.purchase = Purchase.objects.create(payment_method=Purchase.CASH, supplier=cls.supplier)
        cls.purchase_item = PurchaseItem(
            purchase=cls.purchase, product=cls.product, quantity=Decimal("20"), cost_price=Decimal("50.00"), sale_price=Decimal("80.00")
        )
        cls.purchase_item.save()
        cls.purchase.refresh_from_db()
        post_purchase_from_pos(
            purchase_id=cls.purchase.id,
            supplier=cls.supplier,
            total_amount=Decimal("1000.00"),
            paid_amount=Decimal("400.00"),
            payment_method="cash",
            purchase_date=timezone.localdate(),
            actor=cls.admin_user,
        )

        # Credit sale of 4.5 kg, 100 paid now.
        cls.sale = Sale.objects.create(sold_by=cls.admin_user, payment_method=Sale.CASH, is_credit=True, customer=cls.customer)
        cls.sale_item = SaleItem.objects.create(sale=cls.sale, product=cls.product, quantity=Decimal("4.5"), price=Decimal("80.00"))
        cls.sale.refresh_from_db()
        post_sale_from_pos(
            sale_id=cls.sale.id,
            customer=cls.customer,
            total_amount=Decimal("360.00"),
            paid_amount=Decimal("100.00"),
            payment_method="cash",
            sale_date=timezone.localdate(),
        )

        # Customer returns 1 kg against the receivable; 2 kg go back to the supplier against the payable.
        create_sale_return(sale=cls.sale, quantities={cls.sale_item.id: "1"}, refund_method="receivable", reason="Damp", actor=cls.admin_user)
        create_purchase_return(
            purchase=cls.purchase, quantities={cls.purchase_item.id: "2"}, settlement_method="payable", reason="Weevils", actor=cls.admin_user
        )

        # Rent bill, partly paid.
        cls.rent = ExpenseCategory.objects.create(name="Rent")
        cls.expense = Expense.objects.create(category=cls.rent, title="Shop rent", amount=Decimal("300.00"), paid_amount=Decimal("120.00"))
        post_expense(cls.expense.id, actor=cls.admin_user)

        # Settlements both ways.
        create_receipt(party=cls.customer, amount=Decimal("60.00"), payment_method="cash", actor=cls.admin_user)
        create_payment(party=cls.supplier, amount=Decimal("150.00"), payment_method="cash", actor=cls.admin_user)


class AccountingIdentityTests(BooksFixtureMixin, AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_books()

    def test_every_voucher_balances(self):
        self.assertGreaterEqual(Voucher.objects.count(), 7)
        for voucher in Voucher.objects.all():
            self.assertEqual(voucher.total_debit, voucher.total_credit, voucher.narration)

    def test_trial_balance_debits_equal_credits(self):
        tb = get_trial_balance()
        self.assertTrue(tb["balanced"])
        self.assertEqual(tb["total_debit"], tb["total_credit"])
        self.assertGreater(tb["total_debit"], 0)
        self.assertEqual(
            sum(row["balance_debit"] for row in tb["rows"]),
            sum(row["balance_credit"] for row in tb["rows"]),
        )

    def test_balance_sheet_identity_includes_current_earnings(self):
        bs = get_balance_sheet()
        pl = get_profit_and_loss()
        self.assertTrue(bs["balanced"])
        self.assertEqual(bs["total_assets"], bs["total_liabilities"] + bs["total_equity"])
        self.assertEqual(bs["net_profit"], pl["net_profit"])
        # 3.5 kg sold net of the return at 30 margin, less 300 rent.
        self.assertEqual(pl["net_profit"], Decimal("3.5") * Decimal("30") - Decimal("300"))

    def test_control_accounts_agree_with_party_ledgers(self):
        bs = get_balance_sheet()
        receivable = next(row["balance"] for row in bs["assets"] if row["account"] == "Accounts Receivable")
        payable = next(row["balance"] for row in bs["liabilities"] if row["account"] == "Accounts Payable")
        self.assertEqual(receivable, Decimal("120.00"))  # 260 credit - 80 return - 60 receipt
        self.assertEqual(payable, Decimal("350.00"))  # 600 credit - 100 return - 150 payment
        self.assertEqual(get_party_balance(self.customer), receivable)
        self.assertEqual(-get_party_balance(self.supplier), payable)

    def test_identity_survives_opening_balances_and_inactive_accounts(self):
        Account.objects.filter(title="Cash").update(opening_balance=Decimal("500.00"))
        Account.objects.filter(title="Owner Capital").update(opening_balance=Decimal("500.00"))
        Account.objects.filter(title="Sales").update(is_active=False)

        tb = get_trial_balance()
        bs = get_balance_sheet()
        self.assertTrue(tb["balanced"])
        self.assertEqual(tb["opening_difference"], 0)
        self.assertTrue(bs["balanced"])
        sales_row = next(row for row in tb["rows"] if row["account"] == "Sales")
        self.assertFalse(sales_row["is_active"])
        self.assertIn("Sales", [row["account"] for row in get_profit_and_loss()["income"]])
        cash = next(row["balance"] for row in bs["assets"] if row["account"] == "Cash")
        # 500 opening - 400 purchase + 100 sale - 120 rent + 60 receipt - 150 payment
        self.assertEqual(cash, Decimal("-10.00"))

    def test_one_sided_opening_balance_is_reported_not_hidden(self):
        Account.objects.filter(title="Cash").update(opening_balance=Decimal("250.00"))

        tb = get_trial_balance()
        bs = get_balance_sheet()
        self.assertEqual(tb["opening_difference"], Decimal("250.00"))
        self.assertEqual(tb["total_debit"], tb["total_credit"])
        self.assertEqual(bs["opening_difference"], Decimal("250.00"))
        self.assertEqual(bs["total_assets"], bs["total_liabilities"] + bs["total_equity"])

    def test_trial_balance_as_of_date_excludes_later_postings(self):
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        self.assertEqual(get_trial_balance(as_of_date=yesterday)["rows"], [])
        self.assertTrue(get_trial_balance(as_of_date=timezone.localdate())["rows"])

    def test_bad_dates_never_break_a_report(self):
        for name, query in (
            ("trial_balance", {"as_of": "not-a-date"}),
            ("profit_loss", {"start": "2026-13-45", "end": "garbage"}),
            ("balance_sheet", {"as_of": "31/12/2025"}),
            ("day_book", {"start": "x", "end": "y"}),
            ("cash_flow", {"start": "0000-00-00"}),
            ("receivables_aging", {"as_of": "tomorrow"}),
            ("payables_aging", {"as_of": "''"}),
            ("expense_by_category", {"start": "2026-02-30"}),
        ):
            response = self.client.get(reverse(name), query)
            self.assertEqual(response.status_code, 200, name)
        self.assertEqual(self.client.get(reverse("profit_loss"), {"start": "bad"}).context["net_profit"], get_profit_and_loss()["net_profit"])

    def test_reversed_range_is_swapped(self):
        today = timezone.localdate()
        earlier = today - datetime.timedelta(days=5)
        pl = get_profit_and_loss(start_date=today, end_date=earlier)
        self.assertEqual((pl["start_date"], pl["end_date"]), (earlier, today))


class LedgerTests(BooksFixtureMixin, AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_books()

    def test_account_ledger_includes_opening_balance_and_date_filter(self):
        cash = Account.objects.get(title="Cash")
        cash.opening_balance = Decimal("500.00")
        cash.save(update_fields=["opening_balance"])

        data = get_account_ledger(cash)
        self.assertEqual(data["opening"], Decimal("500.00"))
        self.assertEqual(data["closing"], Decimal("-10.00"))
        rows = ledger_rows(data["entries"], data["opening"])
        self.assertEqual(rows[-1]["balance"], Decimal("10.00"))
        self.assertEqual(rows[-1]["drcr"], "Cr")

        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        later = get_account_ledger(cash, start=tomorrow)
        self.assertEqual(later["opening"], Decimal("-10.00"))
        self.assertEqual(later["entries"].count(), 0)

    def test_account_ledger_paginates_with_correct_running_balance(self):
        cash = Account.objects.get(title="Cash")
        capital = Account.objects.get(title="Owner Capital")
        before = get_account_ledger(cash)["closing"]
        for _ in range(55):
            voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL, narration="Top up")
            VoucherLine.objects.create(voucher=voucher, account=cash, debit=Decimal("1.00"))
            VoucherLine.objects.create(voucher=voucher, account=capital, credit=Decimal("1.00"))

        first = self.client.get(reverse("account_ledger", args=[cash.id]))
        second = self.client.get(reverse("account_ledger", args=[cash.id]), {"page": 2})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.context["ledger"]), 50)
        self.assertEqual(first.context["page_obj"].paginator.num_pages, 2)
        last_row = second.context["ledger"][-1]
        signed = last_row["balance"] if last_row["drcr"] == "Dr" else -last_row["balance"]
        self.assertEqual(signed, before + Decimal("55.00"))
        self.assertEqual(second.context["closing"], before + Decimal("55.00"))

    def test_party_statement_has_filters_pagination_and_narration(self):
        url = reverse("party_statement", args=["customer", self.customer.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["party"], self.customer)
        self.assertEqual(response.context["closing"], Decimal("120.00"))
        self.assertContains(response, "Add opening balance")

        tomorrow = (timezone.localdate() + datetime.timedelta(days=1)).isoformat()
        later = self.client.get(url, {"start": tomorrow})
        self.assertEqual(later.context["opening"], Decimal("120.00"))
        self.assertEqual(later.context["rows"], [])

    def test_party_statement_rejects_unknown_party_types(self):
        self.assertEqual(self.client.get("/accounts/party/user/1/").status_code, 404)
        self.assertEqual(self.client.get(reverse("party_statement", args=["customer", 999999])).status_code, 404)

    def test_party_ledger_service_matches_party_balance(self):
        data = get_party_ledger(self.supplier)
        self.assertEqual(data["closing"], get_party_balance(self.supplier))
        self.assertEqual(data["closing_side"], "Cr")


class CsvExportTests(BooksFixtureMixin, AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_books()

    def test_every_report_exports_excel_friendly_csv(self):
        cash = Account.objects.get(title="Cash")
        targets = [
            reverse("trial_balance"),
            reverse("profit_loss"),
            reverse("balance_sheet"),
            reverse("day_book"),
            reverse("cash_flow"),
            reverse("receivables_aging"),
            reverse("payables_aging"),
            reverse("expense_by_category"),
            reverse("account_ledger", args=[cash.id]),
            reverse("party_statement", args=["supplier", self.supplier.id]),
        ]
        for url in targets:
            response = self.client.get(url, {"export": "csv"})
            self.assertEqual(response.status_code, 200, url)
            self.assertTrue(response["Content-Type"].startswith("text/csv"), url)
            self.assertIn("attachment;", response["Content-Disposition"])
            # Exactly one UTF-8 BOM, at the very start.
            self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"), url)
            self.assertEqual(response.content.count(b"\xef\xbb\xbf"), 1, url)
            self.assertGreater(len(_csv_rows(response)), 1, url)

    def test_trial_balance_csv_totals_match(self):
        rows = _csv_rows(self.client.get(reverse("trial_balance"), {"export": "csv"}))
        self.assertEqual(rows[0][:2], ["Account", "Type"])
        total = rows[-1]
        self.assertEqual(total[0], "Total")
        self.assertEqual(total[3], total[4])

    def test_csv_neutralises_formula_cells(self):
        Customer.objects.filter(pk=self.customer.pk).update(name="=HYPERLINK(1)")
        rows = _csv_rows(self.client.get(reverse("receivables_aging"), {"export": "csv"}))
        self.assertEqual(rows[1][0], "'=HYPERLINK(1)")


class AgingTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ensure_default_accounts()
        cls.customer = Customer.objects.create(name="Old Debtor")
        cls.supplier = Supplier.objects.create(name="Patient Supplier")
        cls.today = timezone.localdate()

    def _entry(self, party, days_ago, *, debit=0, credit=0):
        voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=self.today - datetime.timedelta(days=days_ago))
        return PartyLedgerEntry.objects.create(
            voucher=voucher,
            content_type=ContentType.objects.get_for_model(party),
            object_id=party.id,
            debit=Decimal(debit),
            credit=Decimal(credit),
        )

    def test_receivables_are_bucketed_by_age_after_fifo_payments(self):
        self._entry(self.customer, 120, debit="100")
        self._entry(self.customer, 70, debit="200")
        self._entry(self.customer, 40, debit="300")
        self._entry(self.customer, 5, debit="400")
        self._entry(self.customer, 2, credit="150")  # clears the oldest 100 and 50 of the next

        data = get_aging(RECEIVABLE)
        row = data["rows"][0]
        self.assertEqual(row["over_90"], Decimal("0"))
        self.assertEqual(row["days_61_90"], Decimal("150"))
        self.assertEqual(row["days_31_60"], Decimal("300"))
        self.assertEqual(row["current"], Decimal("400"))
        self.assertEqual(row["outstanding"], Decimal("850"))
        self.assertEqual(row["outstanding"], get_party_balance(self.customer))
        self.assertEqual(data["totals"]["outstanding"], Decimal("850"))

    def test_bucket_edges(self):
        for days, key in ((0, "current"), (30, "current"), (31, "days_31_60"), (60, "days_31_60"), (61, "days_61_90"), (90, "days_61_90"), (91, "over_90")):
            customer = Customer.objects.create(name=f"Edge {days}")
            self._entry(customer, days, debit="10")
            row = next(r for r in get_aging(RECEIVABLE)["rows"] if r["party_id"] == customer.id)
            self.assertEqual(row[key], Decimal("10"), days)

    def test_payables_use_credits_and_overpayment_is_an_advance(self):
        self._entry(self.supplier, 95, credit="500")
        self._entry(self.supplier, 10, debit="700")

        row = get_aging(PAYABLE)["rows"][0]
        self.assertEqual(row["outstanding"], Decimal("0"))
        self.assertEqual(row["advance"], Decimal("200"))

    def test_as_of_date_ignores_later_entries(self):
        self._entry(self.customer, 50, debit="100")
        self._entry(self.customer, 1, credit="100")

        as_of = self.today - datetime.timedelta(days=10)
        row = get_aging(RECEIVABLE, as_of_date=as_of)["rows"][0]
        self.assertEqual(row["days_31_60"], Decimal("100"))
        self.assertEqual(get_aging(RECEIVABLE)["rows"], [])


class OtherReportTests(BooksFixtureMixin, AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_books()

    def test_day_book_defaults_to_today_and_balances(self):
        data = get_day_book()
        self.assertEqual(data["start_date"], timezone.localdate())
        self.assertEqual(data["voucher_count"], Voucher.objects.count())
        self.assertEqual(data["total_debit"], data["total_credit"])
        self.assertEqual(get_day_book(voucher_type=Voucher.EXPENSE)["voucher_count"], 1)
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        self.assertEqual(get_day_book(yesterday, yesterday)["voucher_count"], 0)

    def test_day_book_page_filters_by_type(self):
        response = self.client.get(reverse("day_book"), {"type": Voucher.SALE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["voucher_count"], 1)
        self.assertContains(response, "Sale INV-")

    def test_cash_flow_reconciles_with_the_cash_ledger(self):
        data = get_cash_flow()
        cash = Account.objects.get(title="Cash")
        self.assertEqual(data["opening_balance"], Decimal("0.00"))
        self.assertEqual(data["closing_balance"], get_account_ledger(cash)["closing"])
        self.assertEqual(data["closing_balance"], Decimal("-510.00"))
        self.assertEqual(data["net_change"], data["total_in"] - data["total_out"])
        labels = {row["label"]: row for row in data["rows"]}
        self.assertEqual(labels["Sales"]["money_in"], Decimal("100.00"))
        self.assertEqual(labels["Purchases"]["money_out"], Decimal("400.00"))
        self.assertEqual(labels["Customer receipts"]["money_in"], Decimal("60.00"))
        self.assertEqual(labels["Supplier payments"]["money_out"], Decimal("150.00"))
        self.assertEqual(labels["Expenses"]["money_out"], Decimal("120.00"))

    def test_cash_flow_carries_the_opening_balance_forward(self):
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        data = get_cash_flow(tomorrow, tomorrow)
        self.assertEqual(data["opening_balance"], Decimal("-510.00"))
        self.assertEqual(data["rows"], [])

    def test_expense_by_category(self):
        utilities = ExpenseCategory.objects.create(name="Utilities")
        Expense.objects.create(category=utilities, title="Power", amount=Decimal("100.00"), paid_amount=Decimal("100.00"))

        data = get_expense_by_category()
        self.assertEqual([row["category"] for row in data["rows"]], ["Rent", "Utilities"])
        self.assertEqual(data["total_amount"], Decimal("400.00"))
        self.assertEqual(data["total_due"], Decimal("180.00"))
        self.assertEqual(data["rows"][0]["share"], Decimal("75.0"))

    def test_overview_links_every_report(self):
        response = self.client.get(reverse("accounts_home"))
        for name in ("trial_balance", "profit_loss", "balance_sheet", "day_book", "cash_flow", "receivables_aging", "payables_aging", "expense_by_category"):
            self.assertContains(response, reverse(name))
        self.assertContains(response, "Books lock")

    def test_expense_log_is_paginated(self):
        for index in range(30):
            Expense.objects.create(category=self.rent, title=f"Bill {index}", amount=Decimal("10.00"), paid_amount=Decimal("10.00"))
        response = self.client.get(reverse("expense_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["expenses"].paginator.count, 31)
        self.assertEqual(len(response.context["expenses"]), 25)
        second = self.client.get(reverse("expense_list"), {"page": 2})
        self.assertEqual(len(second.context["expenses"]), 6)
        self.assertEqual(response.context["outstanding_total"], Decimal("180.00"))


class PartyOpeningBalanceTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ensure_default_accounts()
        cls.customer = Customer.objects.create(name="Walk-in Regular")
        cls.supplier = Supplier.objects.create(name="Wholesaler")

    def test_customer_opening_receivable_posts_voucher_and_party_entry(self):
        voucher = set_party_opening_balance(party=self.customer, amount="750", direction=THEY_OWE_US, actor=self.admin_user)

        self.assertEqual(voucher.total_debit, voucher.total_credit)
        self.assertEqual(voucher.created_by, self.admin_user)
        lines = {line.account.title: line for line in voucher.lines.select_related("account")}
        self.assertEqual(lines["Accounts Receivable"].debit, Decimal("750.00"))
        self.assertEqual(lines["Opening Balance Equity"].credit, Decimal("750.00"))
        self.assertEqual(get_party_balance(self.customer), Decimal("750.00"))
        self.assertTrue(get_balance_sheet()["balanced"])

    def test_supplier_opening_payable(self):
        voucher = set_party_opening_balance(party=self.supplier, amount="1200.50", direction=WE_OWE_THEM)

        lines = {line.account.title: line for line in voucher.lines.select_related("account")}
        self.assertEqual(lines["Accounts Payable"].credit, Decimal("1200.50"))
        self.assertEqual(lines["Opening Balance Equity"].debit, Decimal("1200.50"))
        self.assertEqual(get_party_balance(self.supplier), Decimal("-1200.50"))
        self.assertEqual(get_aging(PAYABLE)["totals"]["outstanding"], Decimal("1200.50"))

    def test_saving_again_replaces_and_zero_removes(self):
        set_party_opening_balance(party=self.customer, amount="100", direction=THEY_OWE_US)
        set_party_opening_balance(party=self.customer, amount="40", direction=WE_OWE_THEM)

        self.assertEqual(Voucher.objects.filter(ref_model="CustomerOpeningBalance").count(), 1)
        self.assertEqual(get_party_balance(self.customer), Decimal("-40.00"))
        self.assertEqual(get_party_opening(self.customer)["direction"], WE_OWE_THEM)

        self.assertIsNone(set_party_opening_balance(party=self.customer, amount="0", direction=THEY_OWE_US))
        self.assertIsNone(get_party_opening(self.customer))
        self.assertEqual(get_party_balance(self.customer), Decimal("0"))

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            set_party_opening_balance(party=self.customer, amount="-5", direction=THEY_OWE_US)
        with self.assertRaises(ValueError):
            set_party_opening_balance(party=self.customer, amount="5", direction="sideways")
        with self.assertRaises(ValueError):
            set_party_opening_balance(party=self.admin_user, amount="5", direction=THEY_OWE_US)

    def test_view_saves_and_returns_to_the_statement(self):
        statement = reverse("party_statement", args=["supplier", self.supplier.id])
        response = self.client.post(
            reverse("party_opening_balance", args=["supplier", self.supplier.id]),
            {"amount": "300", "direction": WE_OWE_THEM, "date": timezone.localdate().isoformat(), "next": statement},
        )
        self.assertRedirects(response, statement)
        self.assertEqual(get_party_balance(self.supplier), Decimal("-300.00"))
        self.assertContains(self.client.get(statement), "Edit opening balance")

    def test_view_ignores_offsite_next(self):
        response = self.client.post(
            reverse("party_opening_balance", args=["customer", self.customer.id]),
            {"amount": "10", "direction": THEY_OWE_US, "date": timezone.localdate().isoformat(), "next": "https://evil.example/"},
        )
        self.assertRedirects(response, reverse("party_statement", args=["customer", self.customer.id]))

    def test_standalone_page_renders(self):
        response = self.client.get(reverse("party_opening_balance", args=["customer", self.customer.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Opening Balance")


class BooksLockTests(BooksFixtureMixin, AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.build_books()

    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()

    def test_no_new_voucher_on_or_before_the_lock(self):
        set_books_lock(self.today, actor=self.admin_user)

        with self.assertRaisesMessage(ValueError, "books are locked"):
            Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=self.today)
        with self.assertRaisesMessage(ValueError, "books are locked"):
            create_receipt(party=self.customer, amount=Decimal("5"), payment_method="cash")
        with self.assertRaisesMessage(ValueError, "books are locked"):
            set_party_opening_balance(party=self.customer, amount="5", direction=THEY_OWE_US)

        # the pos services go through the same guard without knowing about it
        with self.assertRaisesMessage(ValueError, "books are locked"):
            with transaction.atomic():
                create_sale_return(sale=self.sale, quantities={self.sale_item.id: "1"}, refund_method="receivable", reason="Late", actor=self.admin_user)

    def test_locked_voucher_cannot_be_rewritten(self):
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=self.sale.id)
        lines_before = list(voucher.lines.values_list("account__title", "debit", "credit"))
        set_books_lock(self.today)

        with self.assertRaisesMessage(ValueError, "books are locked"):
            post_sale_from_pos(
                sale_id=self.sale.id,
                customer=self.customer,
                total_amount=Decimal("999.00"),
                paid_amount=Decimal("999.00"),
                payment_method="cash",
                sale_date=self.today + datetime.timedelta(days=1),
            )
        with self.assertRaisesMessage(ValueError, "books are locked"):
            post_expense(self.expense.id)

        self.assertEqual(list(voucher.lines.values_list("account__title", "debit", "credit")), lines_before)

    def test_locked_lines_cannot_be_added_changed_or_deleted(self):
        voucher = Voucher.objects.get(ref_model="Sale", ref_id=self.sale.id)
        cash = Account.objects.get(title="Cash")
        set_books_lock(self.today)

        # Django's deleter marks the transaction broken when a guard raises inside
        # it, which is what makes a real request roll back as a whole.
        with self.assertRaises(ValueError), transaction.atomic():
            VoucherLine.objects.create(voucher=voucher, account=cash, debit=Decimal("1"))
        with self.assertRaises(ValueError), transaction.atomic():
            voucher.lines.all().delete()
        with self.assertRaises(ValueError), transaction.atomic():
            voucher.party_entries.all().delete()
        with self.assertRaises(ValueError), transaction.atomic():
            voucher.delete()
        self.assertTrue(Voucher.objects.filter(pk=voucher.pk).exists())

    def test_entries_after_the_lock_still_post(self):
        set_books_lock(self.today - datetime.timedelta(days=1))
        voucher = create_receipt(party=self.customer, amount=Decimal("5"), payment_method="cash")
        self.assertEqual(voucher.date, self.today)

    def test_lock_cannot_be_in_the_future_and_can_be_reopened(self):
        with self.assertRaises(ValueError):
            set_books_lock(self.today + datetime.timedelta(days=1))
        set_books_lock(self.today)
        set_books_lock(None)
        Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=self.today)
        self.assertEqual(AccountingSettings.objects.count(), 1)

    def test_admin_locks_books_from_the_overview(self):
        response = self.client.post(reverse("books_lock_update"), {"books_locked_until": self.today.isoformat()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AccountingSettings.load().books_locked_until, self.today)
        self.assertEqual(AccountingSettings.load().locked_by, self.admin_user)
        self.assertContains(self.client.get(reverse("accounts_home")), "Locked up to")

        self.client.post(reverse("books_lock_update"), {"books_locked_until": ""})
        self.assertIsNone(AccountingSettings.load().books_locked_until)

    def test_expense_form_reports_the_lock_instead_of_crashing(self):
        set_books_lock(self.today)
        response = self.client.post(
            reverse("expense_list"),
            {
                "action": "add_expense",
                "expense-category": str(self.rent.id),
                "expense-title": "Late bill",
                "expense-date": self.today.isoformat(),
                "expense-amount": "50.00",
                "expense-paid_amount": "0",
                "expense-payment_method": "cash",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "books are locked")
        self.assertFalse(Expense.objects.filter(title="Late bill").exists())


class LedgerIntegrityTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        ensure_default_accounts()
        cls.cash = Account.objects.get(title="Cash")

    def test_voucher_line_must_be_one_sided_and_positive(self):
        voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL)
        for debit, credit in (("0", "0"), ("5", "5"), ("-5", "0"), ("0", "-5")):
            with self.assertRaises(IntegrityError), transaction.atomic():
                VoucherLine.objects.create(voucher=voucher, account=self.cash, debit=Decimal(debit), credit=Decimal(credit))
        VoucherLine.objects.create(voucher=voucher, account=self.cash, debit=Decimal("5"))

    def test_ledger_models_are_read_only_in_admin(self):
        request = RequestFactory().get("/admin/")
        request.user = get_user_model().objects.create_superuser("root", "root@example.com", "RootPass123!")
        for model in (Voucher, VoucherLine, PartyLedgerEntry):
            model_admin = admin_site._registry[model]
            self.assertFalse(model_admin.has_add_permission(request), model)
            self.assertFalse(model_admin.has_change_permission(request), model)
            self.assertFalse(model_admin.has_delete_permission(request), model)

    def test_admin_changelists_and_searches_work(self):
        self.client.force_login(get_user_model().objects.create_superuser("root2", "root2@example.com", "RootPass123!"))
        for name in ("voucher", "voucherline", "partyledgerentry", "loantransactions", "loans", "settlement", "accountingsettings"):
            response = self.client.get(reverse(f"admin:accounts_{name}_changelist"), {"q": "1"})
            self.assertEqual(response.status_code, 200, name)

    def test_voucher_records_who_posted_it(self):
        customer = Customer.objects.create(name="Payer")
        explicit = create_receipt(party=customer, amount=Decimal("10"), payment_method="cash", actor=self.admin_user)
        self.assertEqual(explicit.created_by, self.admin_user)

        with acting_as(self.admin_user):
            ambient = Voucher.objects.create(voucher_type=Voucher.JOURNAL)
        self.assertEqual(ambient.created_by, self.admin_user)
        self.assertIsNone(Voucher.objects.create(voucher_type=Voucher.JOURNAL).created_by)

    def test_settlement_posts_exactly_one_voucher(self):
        customer = Customer.objects.create(name="Single")
        create_receipt(party=customer, amount=Decimal("25"), payment_method="cash")
        settlement = Settlement.objects.get()
        self.assertIsNotNone(settlement.voucher_id)
        self.assertEqual(Voucher.objects.filter(ref_model="Settlement", ref_id=settlement.id).count(), 1)

    def test_voucher_date_is_a_plain_local_date(self):
        voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=timezone.now())
        self.assertIs(type(voucher.date), datetime.date)
        self.assertEqual(Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=None).date, timezone.localdate())
