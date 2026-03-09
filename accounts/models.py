from django.conf import settings
from django.utils import timezone
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Sum
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType



class Account(models.Model):
    """
    Chart of Accounts
    """
    ASSET = "asset"
    LIABILITY = "liability"
    INCOME = "income"
    EXPENSE = "expense"
    EQUITY = "equity"

    TYPE_CHOICES = (
        (ASSET, "Asset"),
        (LIABILITY, "Liability"),
        (INCOME, "Income"),
        (EXPENSE, "Expense"),
        (EQUITY, "Equity"),
    )

    title = models.CharField(max_length=150, unique=True)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    is_active = models.BooleanField(default=True)
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.title} ({self.type})"


def _bank_account_title(name, account_number):
    suffix = (account_number or "").strip()
    suffix = suffix[-4:] if suffix else "ledger"
    return f"Bank - {name} ({suffix})"


class BankAccount(models.Model):
    name = models.CharField(max_length=150)
    account_number = models.CharField(max_length=80)
    branch = models.CharField(max_length=150, blank=True)
    account = models.OneToOneField(
        "Account",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bank_ledger",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "account_number"]
        unique_together = ("name", "account_number")

    @property
    def display_name(self):
        base = f"{self.name} ({self.account_number})"
        if self.branch:
            return f"{base} - {self.branch}"
        return base

    def clean(self):
        errors = {}
        if not (self.name or "").strip():
            errors["name"] = "Bank name is required."
        if not (self.account_number or "").strip():
            errors["account_number"] = "Account number is required."
        if self.account and self.account.type != Account.ASSET:
            errors["account"] = "Bank ledger account must be an asset account."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        desired_title = _bank_account_title(self.name, self.account_number)
        if not self.account_id:
            account, _ = Account.objects.get_or_create(
                title=desired_title,
                defaults={"type": Account.ASSET},
            )
            if account.type != Account.ASSET:
                raise ValidationError({"name": "An account with this title already exists and is not an asset account."})
            if not account.is_active:
                account.is_active = True
                account.save(update_fields=["is_active"])
            self.account = account
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name


class Voucher(models.Model):
    """
    Voucher Header: one voucher has many voucher lines
    """
    SALE = "sale"
    PURCHASE = "purchase"
    RECEIPT = "receipt"
    PAYMENT = "payment"
    JOURNAL = "journal"
    LOAN = "loan"
    EXPENSE = "expense"

    VOUCHER_TYPE_CHOICES = (
        (SALE, "Sale"),
        (PURCHASE, "Purchase"),
        (RECEIPT, "Receipt"),
        (PAYMENT, "Payment"),
        (JOURNAL, "Journal"),
        (LOAN, "Loan"),
        (EXPENSE, "Expense"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    date = models.DateField(default=timezone.now)

    voucher_type = models.CharField(max_length=20, choices=VOUCHER_TYPE_CHOICES)
    narration = models.CharField(max_length=255, blank=True)

    # Optional references (we’ll use later when integrating with POS)
    ref_model = models.CharField(max_length=50, blank=True, null=True)  # e.g. "Sale"
    ref_id = models.PositiveIntegerField(null=True, blank=True)  # e.g. sale.id

    def __str__(self):
        return f"Voucher #{self.id} ({self.voucher_type})"

    def clean(self):
        # Ensure voucher is balanced (Debit == Credit)
        totals = self.lines.aggregate(
            total_debit=Sum("debit"),
            total_credit=Sum("credit"),
        )
        debit = totals["total_debit"] or 0
        credit = totals["total_credit"] or 0

        if debit != credit:
            raise ValidationError(f"Voucher is not balanced: Debit={debit} Credit={credit}")

    @property
    def total_debit(self):
        return self.lines.aggregate(t=Sum("debit"))["t"] or 0

    @property
    def total_credit(self):
        return self.lines.aggregate(t=Sum("credit"))["t"] or 0


class VoucherLine(models.Model):
    """
    Voucher Lines: each row posts to one account
    """
    voucher = models.ForeignKey(Voucher, related_name="lines", on_delete=models.CASCADE)
    account = models.ForeignKey(Account, on_delete=models.PROTECT)

    debit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def clean(self):
        if self.debit and self.credit:
            raise ValidationError("A line cannot have both Debit and Credit.")
        if self.debit < 0 or self.credit < 0:
            raise ValidationError("Debit/Credit cannot be negative.")
        if self.debit == 0 and self.credit == 0:
            raise ValidationError("Either Debit or Credit must be > 0.")

    def __str__(self):
        return f"{self.account.title} D:{self.debit} C:{self.credit}"

class Loans(models.Model):
    TAKEN = "taken"
    GIVEN = "given"

    LOAN_TYPE_CHOICES = (
        (TAKEN, "Taken"),
        (GIVEN, "Given"),
    )

    # 🔗 Link to POS parties (Customer / Supplier)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    party = GenericForeignKey("content_type", "object_id")

    loan_type = models.CharField(max_length=10, choices=LOAN_TYPE_CHOICES)
    start_date = models.DateField(default=timezone.now)

    notes = models.TextField(blank=True)
    is_closed = models.BooleanField(default=False)

    def __str__(self):
        return f"Loan #{self.id} - {self.party} ({self.loan_type})"

    @property
    def balance(self):
        disbursed = sum(
            t.amount for t in self.transactions.all()
            if t.tx_type == LoanTransactions.DISBURSE
        )
        repaid = sum(
            t.amount for t in self.transactions.all()
            if t.tx_type == LoanTransactions.REPAY
        )
        return disbursed - repaid
    class Meta:
        verbose_name = "Loan"
        verbose_name_plural = "Loans"

class LoanTransactions(models.Model):
    DISBURSE = "disburse"
    REPAY = "repay"

    TX_TYPE_CHOICES = (
        (DISBURSE, "Disbursement"),
        (REPAY, "Repayment"),
    )

    CASH = "cash"
    BANK = "bank"

    PAYMENT_METHOD_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    loan = models.ForeignKey(Loans, related_name="transactions", on_delete=models.CASCADE)
    date = models.DateField(default=timezone.now)  # ✅ FIXED
    tx_type = models.CharField(max_length=10, choices=TX_TYPE_CHOICES)
    payment_method = models.CharField(
        max_length=10,
        choices=PAYMENT_METHOD_CHOICES,
        default=CASH
    )

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)

    voucher = models.ForeignKey(
        "Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.amount <= 0:
            raise ValidationError("Amount must be greater than zero.")

        if self.loan.is_closed:
            raise ValidationError("This loan is closed.")

        if self.tx_type == self.REPAY and self.amount > self.loan.balance:
            raise ValidationError("Repayment cannot exceed outstanding balance.")

    def __str__(self):
        return f"{self.loan} | {self.tx_type} | {self.amount}"
    class Meta:
        verbose_name = "LoanTransaction"
        verbose_name_plural = "LoanTransactions"
        
class AccountingEvent(models.Model):
    """
    Immutable business event that must be posted to accounts.
    """
    SALE = "sale"
    PURCHASE = "purchase"
    PAYMENT = "payment"
    RECEIPT = "receipt"

    EVENT_TYPE_CHOICES = (
        (SALE, "Sale"),
        (PURCHASE, "Purchase"),
        (PAYMENT, "Payment"),
        (RECEIPT, "Receipt"),
    )

    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)

    ref_model = models.CharField(max_length=50)
    ref_id = models.PositiveIntegerField()

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    is_credit = models.BooleanField(default=False)

    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.event_type} | {self.ref_model}#{self.ref_id}"


class Settlement(models.Model):
    """
    Money movement to settle AR/AP:
    - RECEIPT: customer pays us -> Cash/Bank DR, Accounts Receivable CR
    - PAYMENT: we pay supplier -> Accounts Payable DR, Cash/Bank CR
    """

    RECEIPT = "receipt"
    PAYMENT = "payment"

    SETTLEMENT_TYPE_CHOICES = (
        (RECEIPT, "Receipt (Customer paid)"),
        (PAYMENT, "Payment (Paid supplier)"),
    )

    CASH = "cash"
    BANK = "bank"
    PAYMENT_METHOD_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    date = models.DateField(default=timezone.now)
    settlement_type = models.CharField(max_length=10, choices=SETTLEMENT_TYPE_CHOICES)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="settlements")

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    party = GenericForeignKey("content_type", "object_id")

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    notes = models.CharField(max_length=255, blank=True)
    voucher = models.ForeignKey("Voucher", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        errors = {}
        if self.amount <= 0:
            errors["amount"] = "Amount must be greater than zero."
        if self.payment_method == self.CASH:
            self.bank_account = None
        if errors:
            raise ValidationError(errors)


    def __str__(self):
        return f"{self.settlement_type} #{self.id} - {self.party} - {self.amount}"

class PartyLedgerEntry(models.Model):
    """
    Sub-ledger for Customers & Suppliers.
    Breaks AR/AP control accounts per party.
    """

    voucher = models.ForeignKey(
        "Voucher",
        on_delete=models.CASCADE,
        related_name="party_entries"
    )

    # Link to Customer or Supplier
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    party = GenericForeignKey("content_type", "object_id")

    debit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.party} | D:{self.debit} C:{self.credit}"
def _expense_account_title(name):
    normalized = " ".join((name or "").split())
    if not normalized:
        return "Expense"
    if normalized.lower().endswith("expense"):
        return normalized
    return f"{normalized} Expense"


class ExpenseCategory(models.Model):
    name = models.CharField(max_length=120, unique=True)
    account = models.OneToOneField(
        "Account",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expense_category",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def clean(self):
        if self.account and self.account.type != Account.EXPENSE:
            raise ValidationError({"account": "Expense type must be linked to an expense account."})

    def save(self, *args, **kwargs):
        desired_title = _expense_account_title(self.name)
        if not self.account_id:
            account, _ = Account.objects.get_or_create(
                title=desired_title,
                defaults={"type": Account.EXPENSE},
            )
            if account.type != Account.EXPENSE:
                raise ValidationError({"name": "An account with this title already exists and is not an expense account."})
            if not account.is_active:
                account.is_active = True
                account.save(update_fields=["is_active"])
            self.account = account

        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Expense(models.Model):
    CASH = "cash"
    BANK = "bank"
    PAYMENT_METHOD_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    title = models.CharField(max_length=150, blank=True)
    vendor = models.CharField(max_length=150, blank=True)
    date = models.DateField(default=timezone.now)
    due_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="expenses")
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expense_documents",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    @property
    def display_title(self):
        return self.title or self.category.name

    @property
    def balance_due(self):
        balance = (self.amount or 0) - (self.paid_amount or 0)
        return balance if balance > 0 else 0

    @property
    def payment_status(self):
        if self.balance_due <= 0:
            return "paid"
        if self.paid_amount > 0:
            return "partial"
        return "unpaid"

    def clean(self):
        errors = {}
        if self.amount in (None, "") or self.amount <= 0:
            errors["amount"] = "Expense amount must be greater than zero."
        if self.paid_amount in (None, "") or self.paid_amount < 0:
            errors["paid_amount"] = "Paid amount cannot be negative."
        if self.amount not in (None, "") and self.paid_amount not in (None, "") and self.paid_amount > self.amount:
            errors["paid_amount"] = "Paid amount cannot exceed the expense amount."
        if self.payment_method == self.CASH:
            self.bank_account = None
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.display_title} ({self.amount})"

class ExpensePayment(models.Model):
    CASH = "cash"
    BANK = "bank"
    PAYMENT_METHOD_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    expense = models.ForeignKey("Expense", on_delete=models.CASCADE, related_name="payments")
    date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="expense_payments")
    bank_name = models.CharField(max_length=150, blank=True)
    bank_account_number = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="expense_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    @property
    def from_account_title(self):
        if self.payment_method != self.BANK:
            return "Cash"
        if self.bank_account_id:
            return self.bank_account.display_name
        return "Bank"

    @property
    def bank_reference(self):
        if self.payment_method != self.BANK:
            return "-"
        if self.bank_account_id:
            return self.bank_account.display_name
        if self.bank_name and self.bank_account_number:
            return f"{self.bank_name} ({self.bank_account_number})"
        return self.bank_name or self.bank_account_number or "-"

    def clean(self):
        errors = {}
        if self.amount in (None, "") or self.amount <= 0:
            errors["amount"] = "Payment amount must be greater than zero."
        if self.expense_id and self.amount not in (None, "") and self.amount > self.expense.balance_due:
            errors["amount"] = f"Payment amount cannot exceed the remaining due of {self.expense.balance_due}."
        if self.payment_method == self.BANK and not self.bank_account_id and not ((self.bank_name or "").strip() and (self.bank_account_number or "").strip()):
            errors["bank_account"] = "Choose which bank ledger the payment left from."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Expense payment #{self.id} - {self.expense.display_title}"

class CapitalEntry(models.Model):
    CASH = "cash"
    BANK = "bank"
    ACCOUNT_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    source_name = models.CharField(max_length=150, default="Owner Capital")
    date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    deposit_to = models.CharField(max_length=10, choices=ACCOUNT_CHOICES, default=CASH)
    bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="capital_entries")
    bank_name = models.CharField(max_length=150, blank=True)
    bank_account_number = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="capital_entries",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    @property
    def from_account_title(self):
        return "Owner Capital"

    @property
    def to_account_title(self):
        if self.deposit_to != self.BANK:
            return "Cash"
        if self.bank_account_id:
            return self.bank_account.display_name
        return "Bank"

    @property
    def bank_reference(self):
        if self.deposit_to != self.BANK:
            return "-"
        if self.bank_account_id:
            return self.bank_account.display_name
        if self.bank_name and self.bank_account_number:
            return f"{self.bank_name} ({self.bank_account_number})"
        return self.bank_name or self.bank_account_number or "-"

    def clean(self):
        errors = {}
        if not self.source_name.strip():
            errors["source_name"] = "Source name is required."
        if self.amount in (None, "") or self.amount <= 0:
            errors["amount"] = "Amount must be greater than zero."
        if self.deposit_to == self.BANK and not self.bank_account_id and not ((self.bank_name or "").strip() and (self.bank_account_number or "").strip()):
            errors["bank_account"] = "Choose which bank ledger received these funds."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Capital entry #{self.id} - {self.source_name}"

class MoneyTransfer(models.Model):
    CASH = "cash"
    BANK = "bank"
    ACCOUNT_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    date = models.DateField(default=timezone.now)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    from_account = models.CharField(max_length=10, choices=ACCOUNT_CHOICES, default=CASH)
    to_account = models.CharField(max_length=10, choices=ACCOUNT_CHOICES, default=BANK)
    from_bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="money_transfers_out")
    from_bank_name = models.CharField(max_length=150, blank=True)
    from_bank_account_number = models.CharField(max_length=80, blank=True)
    to_bank_account = models.ForeignKey("BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="money_transfers_in")
    to_bank_name = models.CharField(max_length=150, blank=True)
    to_bank_account_number = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="money_transfers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-id"]

    @property
    def from_account_title(self):
        if self.from_account != self.BANK:
            return "Cash"
        if self.from_bank_account_id:
            return self.from_bank_account.display_name
        return "Bank"

    @property
    def to_account_title(self):
        if self.to_account != self.BANK:
            return "Cash"
        if self.to_bank_account_id:
            return self.to_bank_account.display_name
        return "Bank"

    @property
    def from_bank_reference(self):
        if self.from_account != self.BANK:
            return "-"
        if self.from_bank_account_id:
            return self.from_bank_account.display_name
        if self.from_bank_name and self.from_bank_account_number:
            return f"{self.from_bank_name} ({self.from_bank_account_number})"
        return self.from_bank_name or self.from_bank_account_number or "-"

    @property
    def to_bank_reference(self):
        if self.to_account != self.BANK:
            return "-"
        if self.to_bank_account_id:
            return self.to_bank_account.display_name
        if self.to_bank_name and self.to_bank_account_number:
            return f"{self.to_bank_name} ({self.to_bank_account_number})"
        return self.to_bank_name or self.to_bank_account_number or "-"

    def clean(self):
        errors = {}
        if self.amount in (None, "") or self.amount <= 0:
            errors["amount"] = "Transfer amount must be greater than zero."
        if self.from_account == self.BANK and not self.from_bank_account_id and not ((self.from_bank_name or "").strip() and (self.from_bank_account_number or "").strip()):
            errors["from_bank_account"] = "Choose which bank ledger the transfer left from."
        if self.to_account == self.BANK and not self.to_bank_account_id and not ((self.to_bank_name or "").strip() and (self.to_bank_account_number or "").strip()):
            errors["to_bank_account"] = "Choose which bank ledger the transfer entered into."
        same_cash_transfer = self.from_account == self.CASH and self.to_account == self.CASH
        same_bank_transfer = self.from_account == self.BANK and self.to_account == self.BANK and self.from_bank_account_id == self.to_bank_account_id
        if same_cash_transfer or same_bank_transfer:
            errors["to_account"] = "Transfer in and transfer out accounts must be different."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Transfer #{self.id} - {self.from_account_title} to {self.to_account_title}"

class AuditLog(models.Model):
    action = models.CharField(max_length=80)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs")
    target_model = models.CharField(max_length=80, blank=True)
    target_id = models.CharField(max_length=80, blank=True)
    description = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action} @ {self.created_at:%Y-%m-%d %H:%M}"







class BackupSnapshot(models.Model):
    label = models.CharField(max_length=150)
    file_name = models.CharField(max_length=255, unique=True)
    notes = models.TextField(blank=True)
    size_bytes = models.BigIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_backups",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    restored_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def size_kb(self):
        return round((self.size_bytes or 0) / 1024, 2)

    def __str__(self):
        return self.label or self.file_name


