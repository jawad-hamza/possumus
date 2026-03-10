from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from accounts.models import Account, CapitalEntry, Expense, ExpenseCategory, ExpensePayment, MoneyTransfer, Voucher, VoucherLine
from accounts.services.expense_posting import post_expense
from pos.test_support import AuthenticatedAdminTestCase


class AccountsViewTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cash = Account.objects.create(title="Cash", type=Account.ASSET)
        cls.bank = Account.objects.create(title="Bank", type=Account.ASSET)
        cls.sales = Account.objects.create(title="Sales", type=Account.INCOME)
        cls.rent = Account.objects.create(title="Rent Expense", type=Account.EXPENSE)
        cls.expense_payable = Account.objects.create(title="Expense Payable", type=Account.LIABILITY)
        cls.owner_capital = Account.objects.create(title="Owner Capital", type=Account.EQUITY)

        sale_voucher = Voucher.objects.create(voucher_type=Voucher.SALE, narration="Cash sale")
        VoucherLine.objects.create(voucher=sale_voucher, account=cls.cash, debit=Decimal("500.00"))
        VoucherLine.objects.create(voucher=sale_voucher, account=cls.sales, credit=Decimal("500.00"))

        expense_voucher = Voucher.objects.create(voucher_type=Voucher.EXPENSE, narration="Shop rent")
        VoucherLine.objects.create(voucher=expense_voucher, account=cls.rent, debit=Decimal("200.00"))
        VoucherLine.objects.create(voucher=expense_voucher, account=cls.cash, credit=Decimal("200.00"))

    def test_accounts_pages_render_plain_language_views(self):
        home_response = self.client.get(reverse("accounts_home"))
        self.assertEqual(home_response.status_code, 200)
        self.assertContains(home_response, "Money Available Now")
        self.assertContains(home_response, "Operating Bills Due")
        self.assertContains(home_response, "Money Desk")

        expense_response = self.client.get(reverse("expense_list"))
        self.assertEqual(expense_response.status_code, 200)
        self.assertContains(expense_response, "Record Expense")

        money_response = self.client.get(reverse("money_desk"))
        self.assertEqual(money_response.status_code, 200)
        self.assertContains(money_response, "Opening Funds / Owner Capital")
        self.assertContains(money_response, "Transfer Cash and Bank")

        trial_response = self.client.get(reverse("trial_balance"))
        self.assertEqual(trial_response.status_code, 200)
        self.assertContains(trial_response, "Trial Balance")

        profit_response = self.client.get(reverse("profit_loss"))
        self.assertEqual(profit_response.status_code, 200)
        self.assertEqual(profit_response.context["net_profit"], Decimal("300.00"))

        balance_response = self.client.get(reverse("balance_sheet"))
        self.assertEqual(balance_response.status_code, 200)
        self.assertContains(balance_response, "Net Position")
        self.assertContains(home_response, reverse("money_breakdown"))

    def test_money_breakdown_page_shows_cash_and_bank_movements(self):
        bank_voucher = Voucher.objects.create(voucher_type=Voucher.RECEIPT, narration="Bank deposit")
        VoucherLine.objects.create(voucher=bank_voucher, account=self.bank, debit=Decimal("100.00"))
        VoucherLine.objects.create(voucher=bank_voucher, account=self.owner_capital, credit=Decimal("100.00"))

        response = self.client.get(reverse("money_breakdown"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cash_balance"], Decimal("300.00"))
        self.assertEqual(response.context["bank_balance"], Decimal("100.00"))
        self.assertEqual(response.context["available_money"], Decimal("400.00"))
        self.assertContains(response, "Why Is Money Available Now This Much?")
        self.assertContains(response, "Cash sale")
        self.assertContains(response, "Shop rent")
        self.assertContains(response, "Bank deposit")

    def test_account_ledger_page_renders_summary(self):
        response = self.client.get(reverse("account_ledger", args=[self.cash.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Closing Balance")
        self.assertContains(response, "Narration")
        self.assertEqual(response.context["closing_balance"], Decimal("300.00"))

    def test_adding_expense_type_creates_linked_expense_account(self):
        response = self.client.post(
            reverse("expense_list"),
            {
                "action": "add_category",
                "type-name": "Electricity",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense_type = ExpenseCategory.objects.get(name="Electricity")
        self.assertEqual(expense_type.account.type, Account.EXPENSE)
        self.assertEqual(expense_type.account.title, "Electricity Expense")

    def test_partial_expense_posts_liability_and_cash_movement(self):
        expense_type = ExpenseCategory.objects.create(name="Shop Rent")

        response = self.client.post(
            reverse("expense_list"),
            {
                "action": "add_expense",
                "expense-category": str(expense_type.id),
                "expense-title": "March Shop Rent",
                "expense-vendor": "Landlord",
                "expense-date": timezone.localdate().isoformat(),
                "expense-due_date": "",
                "expense-amount": "5000.00",
                "expense-paid_amount": "2000.00",
                "expense-payment_method": "bank",
                "expense-notes": "Balance due next week",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get(title="March Shop Rent")
        voucher = expense.voucher

        self.assertIsNotNone(voucher)
        self.assertTrue(voucher.lines.filter(account=expense_type.account, debit=Decimal("5000.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Bank", credit=Decimal("2000.00")).exists())
        self.assertTrue(voucher.lines.filter(account__title="Expense Payable", credit=Decimal("3000.00")).exists())

        home_response = self.client.get(reverse("accounts_home"))
        self.assertEqual(home_response.context["expense_payable_balance"], Decimal("3000.00"))

    def test_expense_payment_updates_due_and_posts_from_cash(self):
        expense_type = ExpenseCategory.objects.create(name="Electricity")
        expense = Expense.objects.create(
            category=expense_type,
            title="March Electricity",
            vendor="Utility Company",
            date=timezone.localdate(),
            amount=Decimal("1000.00"),
            paid_amount=Decimal("0.00"),
            payment_method=Expense.CASH,
        )
        post_expense(expense.id)

        response = self.client.post(
            reverse("money_desk"),
            {
                "action": "pay_expense",
                "payment-expense": str(expense.id),
                "payment-date": timezone.localdate().isoformat(),
                "payment-amount": "1000.00",
                "payment-payment_method": "cash",
                "payment-bank_name": "",
                "payment-bank_account_number": "",
                "payment-notes": "Paid from drawer",
            },
        )

        self.assertEqual(response.status_code, 302)
        expense.refresh_from_db()
        payment = ExpensePayment.objects.get(expense=expense)
        self.assertEqual(expense.paid_amount, Decimal("1000.00"))
        self.assertEqual(expense.balance_due, Decimal("0.00"))
        self.assertTrue(payment.voucher.lines.filter(account__title="Expense Payable", debit=Decimal("1000.00")).exists())
        self.assertTrue(payment.voucher.lines.filter(account__title="Cash", credit=Decimal("1000.00")).exists())


    def test_expense_page_prefills_and_posts_payment(self):
        expense_type = ExpenseCategory.objects.create(name="Internet")
        expense = Expense.objects.create(
            category=expense_type,
            title="March Internet",
            vendor="ISP",
            date=timezone.localdate(),
            amount=Decimal("2500.00"),
            paid_amount=Decimal("500.00"),
            payment_method=Expense.BANK,
        )
        post_expense(expense.id)

        page_response = self.client.get(reverse("expense_list"), {"expense": expense.id})
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context["selected_expense_id"], str(expense.id))
        self.assertContains(page_response, "Selected Bill")
        self.assertContains(page_response, "March Internet")

        payment_response = self.client.post(
            reverse("expense_list"),
            {
                "action": "pay_expense",
                "payment-expense": str(expense.id),
                "payment-date": timezone.localdate().isoformat(),
                "payment-amount": "2000.00",
                "payment-payment_method": "bank",
                "payment-bank_name": "Meezan Bank",
                "payment-bank_account_number": "44556677",
                "payment-notes": "Cleared from main account",
            },
        )

        self.assertEqual(payment_response.status_code, 302)
        expense.refresh_from_db()
        payment = ExpensePayment.objects.get(expense=expense, amount=Decimal("2000.00"))
        self.assertEqual(expense.paid_amount, Decimal("2500.00"))
        self.assertEqual(expense.balance_due, Decimal("0.00"))
        self.assertTrue(payment.voucher.lines.filter(account__title="Expense Payable", debit=Decimal("2000.00")).exists())
        self.assertTrue(payment.voucher.lines.filter(account__title="Bank", credit=Decimal("2000.00")).exists())
    def test_default_expense_types_use_monthly_patterns(self):
        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ExpenseCategory.objects.get(name="Rent").expense_pattern, ExpenseCategory.MONTHLY_FIXED)
        self.assertEqual(ExpenseCategory.objects.get(name="Electricity").expense_pattern, ExpenseCategory.MONTHLY_VARIABLE)
        self.assertEqual(ExpenseCategory.objects.get(name="Wages").expense_pattern, ExpenseCategory.MONTHLY_FIXED)
        self.assertEqual(ExpenseCategory.objects.get(name="Misc").expense_pattern, ExpenseCategory.CUSTOM)

    def test_expense_page_shows_due_soon_monthly_bill_alert(self):
        today = timezone.localdate()
        due_day = min(today.day + 3, 28)
        ExpenseCategory.objects.create(
            name="Internet Bill",
            expense_pattern=ExpenseCategory.MONTHLY_VARIABLE,
            default_due_day=due_day,
            alert_days_before=7,
        )

        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["expense_alert_count"], 1)
        self.assertContains(response, "Internet Bill")

    def test_selecting_monthly_fixed_type_prefills_due_date_and_amount(self):
        today = timezone.localdate()
        due_day = min(today.day + 2, 28)
        category = ExpenseCategory.objects.create(
            name="Warehouse Rent",
            expense_pattern=ExpenseCategory.MONTHLY_FIXED,
            default_due_day=due_day,
            default_amount=Decimal("5500.00"),
            alert_days_before=7,
        )

        response = self.client.get(reverse("expense_list"), {"category": category.id, "type": category.id})

        self.assertEqual(response.status_code, 200)
        expected_due = today.replace(day=due_day)
        self.assertEqual(response.context["selected_category"], str(category.id))
        self.assertEqual(str(response.context["expense_form"]["due_date"].value()), expected_due.isoformat())
        self.assertEqual(str(response.context["expense_form"]["amount"].value()), "5500.00")
        self.assertContains(response, "Using saved rule")

    def test_recorded_monthly_bill_hides_current_cycle_alert(self):
        today = timezone.localdate()
        due_day = min(today.day + 1, 28)
        category = ExpenseCategory.objects.create(
            name="Generator Maintenance Plan",
            expense_pattern=ExpenseCategory.MONTHLY_FIXED,
            default_due_day=due_day,
            default_amount=Decimal("1500.00"),
            alert_days_before=7,
        )
        Expense.objects.create(
            category=category,
            title=f"{today.strftime('%B')} Generator Maintenance Plan",
            date=today,
            due_date=today.replace(day=due_day),
            amount=Decimal("1500.00"),
            paid_amount=Decimal("0.00"),
            payment_method=Expense.CASH,
        )

        response = self.client.get(reverse("expense_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["expense_alert_count"], 0)
    def test_owner_capital_entry_increases_money_available(self):
        response = self.client.post(
            reverse("money_desk"),
            {
                "action": "capital",
                "capital-source_name": "Owner Capital",
                "capital-date": timezone.localdate().isoformat(),
                "capital-amount": "10000.00",
                "capital-deposit_to": "bank",
                "capital-bank_name": "Meezan Bank",
                "capital-bank_account_number": "1234567890",
                "capital-notes": "Initial funding",
            },
        )

        self.assertEqual(response.status_code, 302)
        entry = CapitalEntry.objects.get(source_name="Owner Capital")
        self.assertTrue(entry.voucher.lines.filter(account__title="Bank", debit=Decimal("10000.00")).exists())
        self.assertTrue(entry.voucher.lines.filter(account__title="Owner Capital", credit=Decimal("10000.00")).exists())

        home_response = self.client.get(reverse("accounts_home"))
        self.assertEqual(home_response.context["available_money"], Decimal("10300.00"))

    def test_transfer_records_in_and_out_accounts_with_bank_number(self):
        response = self.client.post(
            reverse("money_desk"),
            {
                "action": "transfer",
                "transfer-date": timezone.localdate().isoformat(),
                "transfer-amount": "150.00",
                "transfer-from_account": "cash",
                "transfer-from_bank_name": "",
                "transfer-from_bank_account_number": "",
                "transfer-to_account": "bank",
                "transfer-to_bank_name": "HBL",
                "transfer-to_bank_account_number": "99887766",
                "transfer-notes": "Deposit to bank",
            },
        )

        self.assertEqual(response.status_code, 302)
        transfer = MoneyTransfer.objects.get(amount=Decimal("150.00"))
        self.assertTrue(transfer.voucher.lines.filter(account__title="Bank", debit=Decimal("150.00")).exists())
        self.assertTrue(transfer.voucher.lines.filter(account__title="Cash", credit=Decimal("150.00")).exists())
        self.assertIn("HBL (99887766)", transfer.voucher.narration)






