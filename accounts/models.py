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

    # Link to POS party (Customer or Supplier)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    party = GenericForeignKey("content_type", "object_id")

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    notes = models.CharField(max_length=255, blank=True)

    # auto-created accounting voucher
    voucher = models.ForeignKey("Voucher", null=True, blank=True, on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.amount <= 0:
            raise ValidationError("Amount must be greater than zero.")

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