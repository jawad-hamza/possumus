from django import forms
from django.contrib.auth import get_user_model
from django.db.models import F
from django.utils import timezone

from accounts.models import (
    BankAccount,
    CapitalEntry,
    Expense,
    ExpenseCategory,
    ExpensePayment,
    MoneyTransfer,
)
from accounts.receipt_forms import BankReceiptFormMixin
from accounts.services.books_lock import assert_books_open
from accounts.services.cash_bank import bank_account_queryset
from accounts.services.party_opening import DIRECTION_CHOICES, THEY_OWE_US, WE_OWE_THEM


class StyledModelForm(forms.ModelForm):
    def _apply_bootstrap(self):
        from pos.forms import plain_empty_choices

        plain_empty_choices(self.fields)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            elif isinstance(widget, forms.Textarea):
                css = widget.attrs.get("class", "")
                widget.attrs["class"] = (css + " form-control").strip()
                widget.attrs.setdefault("rows", 3)
            else:
                css = widget.attrs.get("class", "")
                widget.attrs["class"] = (css + " form-control").strip()


class BooksLockDateMixin:
    """Says on the date field, in plain words, that the chosen day falls in a closed period."""

    def clean_date(self):
        value = self.cleaned_data.get("date")
        if value:
            try:
                assert_books_open(value, action="record this")
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return value


class BankAccountForm(StyledModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "account_number", "branch", "is_active"]
        labels = {
            "name": "Bank Name",
            "account_number": "Account Number",
            "branch": "Branch / Notes",
            "is_active": "Active Bank Ledger",
        }
        help_texts = {
            "branch": "Optional branch, branch code, or short note to help staff choose the right bank account.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class ExpenseCategoryForm(StyledModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "expense_pattern", "default_due_day", "default_amount", "alert_days_before"]
        labels = {
            "name": "Expense Type",
            "expense_pattern": "Billing Style",
            "default_due_day": "Monthly Due Day",
            "default_amount": "Default Amount",
            "alert_days_before": "Alert Before (days)",
        }
        help_texts = {
            "name": "Examples: Shop Rent, Electricity, Gas, Internet, Packing Material.",
            "expense_pattern": "Use monthly fixed for same-amount bills, monthly variable for bills like electricity, or custom for one-off expenses.",
            "default_due_day": "Optional. Set a calendar day like 1, 5, or 25 to trigger monthly reminders.",
            "default_amount": "Optional. Helpful for fixed bills like rent or wages.",
            "alert_days_before": "How many days before the due date staff should start seeing reminders.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["default_due_day"].required = False
        self.fields["default_amount"].required = False
        self.fields["alert_days_before"].required = False
        self.fields["default_due_day"].widget.attrs.setdefault("min", 1)
        self.fields["default_due_day"].widget.attrs.setdefault("max", 31)
        self.fields["alert_days_before"].widget.attrs.setdefault("min", 0)

    def clean(self):
        cleaned_data = super().clean()
        pattern = cleaned_data.get("expense_pattern")
        due_day = cleaned_data.get("default_due_day")
        default_amount = cleaned_data.get("default_amount")
        alert_days = cleaned_data.get("alert_days_before")

        if pattern == ExpenseCategory.CUSTOM:
            cleaned_data["default_due_day"] = None
            self.instance.default_due_day = None
        if default_amount is not None and default_amount < 0:
            self.add_error("default_amount", "Default amount cannot be negative.")
        if due_day is not None and not 1 <= int(due_day) <= 31:
            self.add_error("default_due_day", "Due day must be between 1 and 31.")
        if alert_days in (None, ""):
            cleaned_data["alert_days_before"] = 7
            self.instance.alert_days_before = 7
        elif int(alert_days) < 0:
            self.add_error("alert_days_before", "Alert days cannot be negative.")
        return cleaned_data


class ExpenseForm(BankReceiptFormMixin, BooksLockDateMixin, StyledModelForm):
    # What is paid now through a bank carries its receipt (see accounts.receipt_forms).
    bank_receipt_amount_field = "paid_amount"

    class Meta:
        model = Expense
        fields = [
            "category",
            "title",
            "vendor",
            "employee",
            "date",
            "due_date",
            "amount",
            "paid_amount",
            "payment_method",
            "bank_account",
            "notes",
        ]
        labels = {
            "title": "Bill Title / Reference",
            "vendor": "Paid To / Vendor",
            "employee": "Employee / Staff",
            "date": "Expense Date",
            "due_date": "Due Date",
            "amount": "Total Amount",
            "paid_amount": "Paid Now",
            "payment_method": "Payment Method",
            "bank_account": "Bank Ledger",
        }
        help_texts = {
            "title": "Optional. Example: March shop rent, February electricity bill.",
            "vendor": "Optional. Example: landlord, utility company, internet provider.",
            "employee": "Optional. Use this when the bill is a wage or staff-related payment so employee reports stay accurate.",
            "paid_amount": "Enter 0 if the bill is still unpaid. The remaining amount becomes a liability.",
            "payment_method": "Used only for the amount paid now.",
            "bank_account": "Optional. Leave blank to post the paid amount to the generic Bank account.",
        }
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True).order_by("name")
        self.fields["bank_account"].queryset = bank_account_queryset()
        self.fields["employee"].queryset = get_user_model().objects.filter(is_active=True).order_by("first_name", "username")
        self.fields["title"].required = False
        self.fields["vendor"].required = False
        self.fields["employee"].required = False
        self.fields["due_date"].required = False
        self.fields["notes"].required = False
        self.fields["bank_account"].required = False

    def clean_paid_amount(self):
        paid_amount = self.cleaned_data["paid_amount"]
        if paid_amount < 0:
            raise forms.ValidationError("Paid amount cannot be negative.")
        return paid_amount

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("payment_method") == Expense.CASH:
            cleaned_data["bank_account"] = None
            self.instance.bank_account = None
        if cleaned_data.get("employee") and not cleaned_data.get("title"):
            cleaned_data["title"] = f"Wages - {cleaned_data['employee'].get_full_name() or cleaned_data['employee'].username}"
            self.instance.title = cleaned_data["title"]
        return cleaned_data


class ExpensePaymentForm(BankReceiptFormMixin, BooksLockDateMixin, StyledModelForm):
    class Meta:
        model = ExpensePayment
        fields = [
            "expense",
            "date",
            "amount",
            "payment_method",
            "bank_account",
            "bank_name",
            "bank_account_number",
            "notes",
        ]
        labels = {
            "expense": "Expense Bill",
            "amount": "Pay Amount",
            "payment_method": "Pay From",
            "bank_account": "Bank Ledger",
            "bank_name": "Bank Name (Legacy)",
            "bank_account_number": "Bank Account Number (Legacy)",
        }
        help_texts = {
            "payment_method": "Choose the account the money is leaving from.",
            "bank_account": "Preferred. Pick the real bank ledger when available.",
            "bank_name": "Optional fallback if you still use the generic Bank account.",
            "bank_account_number": "Optional fallback account number for generic Bank postings.",
        }
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["expense"].queryset = Expense.objects.filter(amount__gt=F("paid_amount")).order_by("date", "id")
        self.fields["bank_account"].queryset = bank_account_queryset()
        self.fields["notes"].required = False
        self.fields["bank_account"].required = False
        self.fields["bank_name"].required = False
        self.fields["bank_account_number"].required = False

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("payment_method") == ExpensePayment.CASH:
            cleaned_data["bank_account"] = None
            cleaned_data["bank_name"] = ""
            cleaned_data["bank_account_number"] = ""
            self.instance.bank_account = None
            self.instance.bank_name = ""
            self.instance.bank_account_number = ""
        return cleaned_data


class CapitalEntryForm(BankReceiptFormMixin, BooksLockDateMixin, StyledModelForm):
    # Money desk entries may carry a bank receipt; it is never demanded.
    bank_receipt_method_fields = ("deposit_to",)
    bank_receipt_required = False

    class Meta:
        model = CapitalEntry
        fields = [
            "source_name",
            "date",
            "amount",
            "deposit_to",
            "bank_account",
            "bank_name",
            "bank_account_number",
            "notes",
        ]
        labels = {
            "source_name": "Source / Introduced By",
            "amount": "Amount Introduced",
            "deposit_to": "Transfer In To",
            "bank_account": "Bank Ledger",
            "bank_name": "Bank Name (Legacy)",
            "bank_account_number": "Bank Account Number (Legacy)",
        }
        help_texts = {
            "source_name": "Examples: Owner Capital, Partner Investment, Personal Cash Introduced.",
            "deposit_to": "This is the account the money is entering into.",
            "bank_account": "Preferred. Pick the real bank ledger when available.",
        }
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["bank_account"].queryset = bank_account_queryset()
        self.fields["notes"].required = False
        self.fields["bank_account"].required = False
        self.fields["bank_name"].required = False
        self.fields["bank_account_number"].required = False

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("deposit_to") == CapitalEntry.CASH:
            cleaned_data["bank_account"] = None
            cleaned_data["bank_name"] = ""
            cleaned_data["bank_account_number"] = ""
            self.instance.bank_account = None
            self.instance.bank_name = ""
            self.instance.bank_account_number = ""
        return cleaned_data


class MoneyTransferForm(BankReceiptFormMixin, BooksLockDateMixin, StyledModelForm):
    bank_receipt_method_fields = ("from_account", "to_account")
    bank_receipt_required = False

    class Meta:
        model = MoneyTransfer
        fields = [
            "date",
            "amount",
            "from_account",
            "from_bank_account",
            "from_bank_name",
            "from_bank_account_number",
            "to_account",
            "to_bank_account",
            "to_bank_name",
            "to_bank_account_number",
            "notes",
        ]
        labels = {
            "amount": "Transfer Amount",
            "from_account": "Transfer Out From",
            "from_bank_account": "From Bank Ledger",
            "from_bank_name": "From Bank Name (Legacy)",
            "from_bank_account_number": "From Account Number (Legacy)",
            "to_account": "Transfer In To",
            "to_bank_account": "To Bank Ledger",
            "to_bank_name": "To Bank Name (Legacy)",
            "to_bank_account_number": "To Account Number (Legacy)",
        }
        help_texts = {
            "from_account": "This is the account money is leaving from.",
            "to_account": "This is the account money is entering into.",
            "from_bank_account": "Preferred. Pick the real bank ledger when available.",
            "to_bank_account": "Preferred. Pick the real bank ledger when available.",
        }
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        queryset = bank_account_queryset()
        self.fields["from_bank_account"].queryset = queryset
        self.fields["to_bank_account"].queryset = queryset
        self.fields["notes"].required = False
        self.fields["from_bank_account"].required = False
        self.fields["from_bank_name"].required = False
        self.fields["from_bank_account_number"].required = False
        self.fields["to_bank_account"].required = False
        self.fields["to_bank_name"].required = False
        self.fields["to_bank_account_number"].required = False

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("from_account") == MoneyTransfer.CASH:
            cleaned_data["from_bank_account"] = None
            cleaned_data["from_bank_name"] = ""
            cleaned_data["from_bank_account_number"] = ""
            self.instance.from_bank_account = None
            self.instance.from_bank_name = ""
            self.instance.from_bank_account_number = ""
        if cleaned_data.get("to_account") == MoneyTransfer.CASH:
            cleaned_data["to_bank_account"] = None
            cleaned_data["to_bank_name"] = ""
            cleaned_data["to_bank_account_number"] = ""
            self.instance.to_bank_account = None
            self.instance.to_bank_name = ""
            self.instance.to_bank_account_number = ""
        return cleaned_data


class BooksLockForm(forms.Form):
    books_locked_until = forms.DateField(
        required=False,
        label="Lock the books up to",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
        help_text="Nothing dated on or before this day can be posted, edited or returned. Leave empty to reopen.",
    )

    def clean_books_locked_until(self):
        value = self.cleaned_data.get("books_locked_until")
        if value and value > timezone.localdate():
            raise forms.ValidationError("The lock date cannot be in the future.")
        return value


class PartyOpeningBalanceForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        label="Opening balance",
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0", "inputmode": "decimal"}),
        help_text="Enter 0 to remove a previously recorded opening balance.",
    )
    direction = forms.ChoiceField(
        choices=DIRECTION_CHOICES,
        label="Who owes whom",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    date = forms.DateField(
        label="As of",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date", "class": "form-control"}),
    )

    def __init__(self, *args, party_kind="customer", **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields["direction"].initial = self.initial.get("direction") or (
                THEY_OWE_US if party_kind == "customer" else WE_OWE_THEM
            )
            self.fields["date"].initial = self.initial.get("date") or timezone.localdate()

    def clean_date(self):
        value = self.cleaned_data.get("date")
        if value and value > timezone.localdate():
            raise forms.ValidationError("An opening balance cannot be dated in the future.")
        try:
            assert_books_open(value, action="record an opening balance")
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return value
