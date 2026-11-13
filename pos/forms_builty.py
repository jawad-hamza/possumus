"""The builty (freight bill) form."""

from decimal import Decimal

from django import forms
from django.utils import timezone

from accounts.receipt_forms import BankReceiptFormMixin
from accounts.services.cash_bank import bank_account_queryset

from .forms import StyledModelForm
from .models import Builty, Supplier


class BuiltyForm(BankReceiptFormMixin, StyledModelForm):
    # A builty paid by bank carries its receipt (see accounts.receipt_forms).
    bank_receipt_method_fields = ("payment_method",)

    class Meta:
        model = Builty
        fields = ["date", "builty_number", "transporter", "amount", "basis", "payment_method", "bank_account", "notes"]
        labels = {
            "date": "Builty date",
            "builty_number": "Builty no.",
            "transporter": "Transporter",
            "amount": "Freight amount",
            "basis": "Share the freight",
            "payment_method": "Paid",
            "bank_account": "Bank ledger",
            "notes": "Notes",
        }
        help_texts = {
            "builty_number": "The number printed on the transporter's receipt (GR / bilty number).",
            "transporter": "The goods transport company. Needed when the freight stays unpaid; it is kept as a supplier so its balance, statement and payments work like any supplier's.",
            "amount": "The freight on the builty. It is shared over the ticked purchases and added to the cost of their goods.",
            "basis": "Per box: each line counts its boxes; a product without boxes counts its cartons, and one without either counts pieces. Per carton and per piece work the same way. By value shares in proportion to each line's amount.",
            "payment_method": "Cash or bank when you paid the transporter now; on the transporter's account when you will pay later.",
            "bank_account": "The bank ledger the freight was paid from.",
        }
        widgets = {
            "date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["transporter"].required = False
        self.fields["transporter"].queryset = Supplier.objects.filter(is_active=True).order_by("name")
        self.fields["transporter"].empty_label = "No transporter (paid now)"
        self.fields["bank_account"].required = False
        self.fields["bank_account"].queryset = bank_account_queryset()
        self.fields["bank_account"].empty_label = "Choose a bank account"
        self.fields["amount"].widget.attrs.update({"min": "0.01", "step": "0.01", "inputmode": "decimal"})
        self.fields["notes"].required = False

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is None or Decimal(amount) <= 0:
            raise forms.ValidationError("Enter the freight amount on the builty.")
        return amount

    def clean_date(self):
        value = self.cleaned_data.get("date")
        if value and value > timezone.localdate():
            raise forms.ValidationError("A builty cannot be dated in the future.")
        return value

    def clean(self):
        cleaned_data = super().clean()
        method = cleaned_data.get("payment_method")
        if method != Builty.BANK:
            cleaned_data["bank_account"] = None
        elif not cleaned_data.get("bank_account"):
            self.add_error("bank_account", "Choose which bank ledger paid the freight.")
        if method == Builty.CREDIT and not cleaned_data.get("transporter"):
            self.add_error("transporter", "Choose the transporter: an unpaid builty is owed to someone.")
        return cleaned_data


class BuiltyReverseForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        label="Reason",
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Why is this builty being reversed?"}),
        error_messages={"required": "Say why the builty is being reversed."},
    )
