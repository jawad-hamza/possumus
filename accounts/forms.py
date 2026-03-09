from django import forms
from django.db.models import F

from accounts.models import (
    BankAccount,
    CapitalEntry,
    Expense,
    ExpenseCategory,
    ExpensePayment,
    MoneyTransfer,
)
from accounts.services.cash_bank import bank_account_queryset


class StyledModelForm(forms.ModelForm):
    def _apply_bootstrap(self):
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
        fields = ["name"]
        labels = {"name": "Expense Type"}
        help_texts = {"name": "Examples: Shop Rent, Electricity, Gas, Internet, Packing Material."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class ExpenseForm(StyledModelForm):
    class Meta:
        model = Expense
        fields = [
            "category",
            "title",
            "vendor",
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
        self.fields["title"].required = False
        self.fields["vendor"].required = False
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
        return cleaned_data


class ExpensePaymentForm(StyledModelForm):
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


class CapitalEntryForm(StyledModelForm):
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


class MoneyTransferForm(StyledModelForm):
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
