"""Form pieces for bank receipts: the upload field, and the mixin a payment form adds."""

from decimal import Decimal

from django import forms

from accounts.services.bank_receipts import (
    ACCEPT_ATTRIBUTE,
    BANK,
    MISSING_MESSAGE,
    UPLOAD_FIELD,
    check_upload,
    has_receipt,
    is_bank_payment,
    receipts_required,
    upload_help_text,
)


class BankReceiptField(forms.FileField):
    """A file input that only accepts a real JPG, PNG, WEBP or PDF. Cleans to a CheckedUpload."""

    widget = forms.FileInput

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("label", "Bank receipt")
        kwargs.setdefault("help_text", upload_help_text())
        super().__init__(**kwargs)
        self.widget.attrs.setdefault("accept", ACCEPT_ATTRIBUTE)

    def clean(self, data, initial=None):
        uploaded = super().clean(data, initial)
        if not uploaded:
            return None
        return check_upload(uploaded)


class BankReceiptFormMixin:
    """Adds the `bank_receipt` upload to a payment form and applies the shop's rule.

    Put it first in the bases. The form says when it records a bank payment:

        bank_receipt_method_fields = ("payment_method",)  # a bank payment when any of these is "bank"
        bank_receipt_amount_field = "paid_amount"         # ...and this amount is above zero (optional)
        bank_receipt_required = True                      # False where a receipt may be added but never must

    After saving, the view hands form.cleaned_data["bank_receipt"] to attach_receipt().
    The view must build the form with request.FILES and the page's <form> must be multipart.
    """

    bank_receipt_method_fields = ("payment_method",)
    bank_receipt_amount_field = None
    bank_receipt_required = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[UPLOAD_FIELD] = BankReceiptField()

    def records_bank_payment(self, cleaned_data) -> bool:
        if not any(cleaned_data.get(name) == BANK for name in self.bank_receipt_method_fields):
            return False
        if self.bank_receipt_amount_field is None:
            return True
        return Decimal(cleaned_data.get(self.bank_receipt_amount_field) or 0) > 0

    def _receipt_still_needed(self) -> bool:
        """False when the record being edited already carries one, or was a bank payment before this edit."""
        instance = getattr(self, "instance", None)
        if instance is None or instance.pk is None:
            return True
        if has_receipt(instance):
            return False
        # A bank payment recorded before the rule existed can still be corrected; its proof
        # is added from the page that shows it. Turning a payment into a bank payment needs one.
        stored = type(instance).objects.filter(pk=instance.pk).first()
        return stored is None or not is_bank_payment(stored)

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data is None:
            cleaned_data = self.cleaned_data
        if not self.records_bank_payment(cleaned_data):
            # A file chosen and then the payment switched to cash: there is nothing to prove.
            cleaned_data[UPLOAD_FIELD] = None
        elif (
            self.bank_receipt_required
            and not cleaned_data.get(UPLOAD_FIELD)
            and UPLOAD_FIELD not in self.errors
            and receipts_required()
            and self._receipt_still_needed()
        ):
            self.add_error(UPLOAD_FIELD, MISSING_MESSAGE)
        return cleaned_data


class BankReceiptUploadForm(forms.Form):
    """The receipt page: add the proof to a payment, or replace the one it carries."""

    bank_receipt = BankReceiptField(
        required=True,
        error_messages={"required": "Choose the receipt file first: a photo, a screenshot or the PDF slip."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[UPLOAD_FIELD].help_text = upload_help_text()
        self.fields[UPLOAD_FIELD].widget.attrs["class"] = "form-control"
