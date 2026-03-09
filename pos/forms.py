from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.forms import BaseModelFormSet, modelformset_factory

from .models import (
    Brand,
    Category,
    CompanyProfile,
    Customer,
    Product,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    SaleReturn,
    StockAdjustment,
    Supplier,
    SystemSettings,
)
from accounts.services.cash_bank import bank_account_queryset
from .services.access import ADMIN_ROLE, CASHIER_ROLE


ROLE_CHOICES = (
    (ADMIN_ROLE, "Admin"),
    (CASHIER_ROLE, "Cashier"),
)


class StyledFieldsMixin:
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


class StyledModelForm(StyledFieldsMixin, forms.ModelForm):
    pass


class LoginForm(StyledFieldsMixin, AuthenticationForm):
    username = forms.CharField(label="Login ID")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["username"].widget.attrs.setdefault("autofocus", True)
        self.fields["username"].widget.attrs.setdefault("placeholder", "Enter login ID")
        self.fields["password"].widget.attrs.setdefault("placeholder", "Enter password")


class FirstAdminSetupForm(StyledFieldsMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["username"].label = "Admin Login ID"
        self.fields["username"].help_text = "Use this ID to sign in as the business owner or administrator."

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        user.is_staff = True
        user.is_active = True
        if commit:
            user.save()
        return user


class TeamUserForm(StyledFieldsMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=ROLE_CHOICES, initial=CASHIER_ROLE)
    is_active = forms.BooleanField(required=False, initial=True)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email", "role", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["username"].label = "Login ID"
        self.fields["username"].help_text = "Unique ID used by the team member to sign in."
        self.fields["role"].help_text = "Admins get full access. Cashiers are limited to POS and their receipts."
        self.fields["password1"].help_text = "Use a strong password with at least 8 characters."

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        user.is_active = self.cleaned_data.get("is_active", True)
        if commit:
            user.save()
        return user


class CompanyProfileForm(StyledModelForm):
    class Meta:
        model = CompanyProfile
        fields = [
            "name",
            "logo",
            "phone",
            "email",
            "address",
            "tax_registration_number",
            "invoice_footer",
            "show_qr",
            "qr_note",
        ]
        labels = {
            "name": "Business Name",
            "tax_registration_number": "GST / Tax Number",
            "invoice_footer": "Receipt Footer",
            "show_qr": "Show QR",
            "qr_note": "QR Note",
        }
        help_texts = {
            "tax_registration_number": "Optional registration shown on receipts and invoices.",
            "invoice_footer": "Printed at the bottom of the sale receipt.",
            "qr_note": "Short note shown below the QR if you use it.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["logo"].widget.attrs.setdefault("class", "form-control")


class SystemSettingsForm(StyledModelForm):
    class Meta:
        model = SystemSettings
        fields = [
            "gst_enabled",
            "tax_mode",
            "tax_label",
            "allow_credit_sales",
            "allow_price_override",
            "allow_line_discount",
            "default_sale_payment_method",
            "thermal_printer_width_mm",
        ]
        labels = {
            "gst_enabled": "Enable GST / Tax",
            "tax_mode": "Tax Mode",
            "tax_label": "Tax Label",
            "allow_credit_sales": "Allow Credit Sales",
            "allow_price_override": "Allow Price Override",
            "allow_line_discount": "Allow Line Discount",
            "default_sale_payment_method": "Default Payment Method",
            "thermal_printer_width_mm": "Thermal Printer Width (mm)",
        }
        help_texts = {
            "tax_mode": "Use category GST to tax each product line from its category. Manual cart tax keeps the old cart-level tax box.",
            "tax_label": "Examples: GST, VAT, Sales Tax.",
            "thermal_printer_width_mm": "50mm is standard for small thermal receipt printers.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_tax_label(self):
        return (self.cleaned_data.get("tax_label") or "GST").strip() or "GST"


class CustomerForm(StyledModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "email", "address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class SupplierForm(StyledModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "phone", "email", "address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class ProductForm(StyledModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "brand",
            "sku",
            "barcode",
            "category",
            "supplier",
            "cost_price",
            "price",
            "minimum_margin_percent",
            "stock",
            "low_stock_threshold",
            "location",
            "is_active",
        ]
        labels = {
            "price": "Sale Price",
            "cost_price": "Current Cost",
            "minimum_margin_percent": "Minimum Margin (%)",
            "location": "Storage Location",
        }
        help_texts = {
            "barcode": "Optional. If blank, SKU can still be scanned.",
            "cost_price": "Used for inventory valuation and gross profit.",
            "price": "Current selling price used on POS checkout.",
            "minimum_margin_percent": "Purchase updates must keep this margin or higher.",
            "location": "Example: Warehouse A / Rack 3",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["brand"].queryset = Brand.objects.filter(is_active=True).order_by("name")
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by("name")


class QuickProductForm(StyledModelForm):
    class Meta:
        model = Product
        fields = [
            "name",
            "sku",
            "barcode",
            "brand",
            "category",
            "supplier",
            "price",
            "minimum_margin_percent",
            "low_stock_threshold",
            "location",
        ]
        labels = {
            "price": "Opening Sale Price",
            "minimum_margin_percent": "Minimum Margin (%)",
            "low_stock_threshold": "Low Stock Alert",
        }
        help_texts = {
            "price": "Use the intended retail price. Purchase will update it later if needed.",
            "barcode": "Optional. Leave blank if you only use SKU.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["brand"].queryset = Brand.objects.filter(is_active=True).order_by("name")
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by("name")
        self.fields["brand"].required = False
        self.fields["category"].required = False
        self.fields["supplier"].required = False
        self.fields["barcode"].required = False
        self.fields["location"].required = False
        self.fields["minimum_margin_percent"].initial = 0
        self.fields["low_stock_threshold"].initial = 5

    def save(self, commit=True):
        product = super().save(commit=False)
        product.stock = 0
        product.cost_price = 0
        product.is_active = True
        if commit:
            product.save()
        return product


class PurchaseForm(StyledModelForm):
    class Meta:
        model = Purchase
        fields = ["supplier", "payment_method", "bank_account", "paid_amount"]
        labels = {
            "supplier": "Supplier",
            "payment_method": "Payment Method",
            "bank_account": "Bank Ledger",
            "paid_amount": "Paid Now",
        }
        help_texts = {
            "supplier": "Keep blank only for a pure cash purchase with no party tracking.",
            "bank_account": "Required only when the paid amount leaves a bank ledger.",
            "paid_amount": "Enter 0 for full credit. Enter the amount paid at the time of purchase.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["supplier"].required = False
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by("name")
        self.fields["bank_account"].required = False
        self.fields["bank_account"].queryset = bank_account_queryset()

    def clean_paid_amount(self):
        paid_amount = self.cleaned_data["paid_amount"]
        if paid_amount < 0:
            raise forms.ValidationError("Paid amount cannot be negative.")
        return paid_amount

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("payment_method") == Purchase.CASH:
            cleaned_data["bank_account"] = None
            self.instance.bank_account = None
        return cleaned_data


class PurchaseItemForm(StyledModelForm):
    class Meta:
        model = PurchaseItem
        fields = ["product", "quantity", "cost_price", "sale_price"]
        labels = {
            "cost_price": "Unit Cost",
            "sale_price": "Sale Price",
        }
        help_texts = {
            "sale_price": "The purchase will update the product's active sale price.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.empty_permitted = True
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("name")


class BrandForm(StyledModelForm):
    class Meta:
        model = Brand
        fields = ["name", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class QuickBrandForm(StyledModelForm):
    class Meta:
        model = Brand
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class CategoryForm(StyledModelForm):
    class Meta:
        model = Category
        fields = ["name", "tax_status", "gst_rate", "is_active"]
        labels = {
            "tax_status": "GST Status",
            "gst_rate": "GST Rate (%)",
        }
        help_texts = {
            "tax_status": "Taxable categories will apply GST automatically in POS when category GST mode is enabled.",
            "gst_rate": "Use 0 for exempt or zero-rated categories.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class QuickCategoryForm(StyledModelForm):
    class Meta:
        model = Category
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class BasePurchaseItemFormSet(BaseModelFormSet):
    def clean(self):
        super().clean()
        active_forms = 0
        for form in self.forms:
            if not getattr(form, "cleaned_data", None):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            if not form.cleaned_data.get("product"):
                continue
            active_forms += 1

        if active_forms == 0:
            raise forms.ValidationError("At least one purchase item is required.")


PurchaseItemFormSet = modelformset_factory(
    PurchaseItem,
    form=PurchaseItemForm,
    formset=BasePurchaseItemFormSet,
    extra=3,
    can_delete=True,
)





class StockAdjustmentForm(StyledFieldsMixin, forms.Form):
    REASON_CHOICES = (
        ("damaged", "Damaged / Expired"),
        ("lost", "Lost / Missing"),
        ("opening_balance", "Opening Balance Correction"),
        ("stock_take_difference", "Stock Take Difference"),
        ("other", "Other"),
    )

    product = forms.ModelChoiceField(queryset=Product.objects.none())
    adjustment_mode = forms.ChoiceField(choices=StockAdjustment.MODE_CHOICES)
    quantity_delta = forms.IntegerField(required=False, label="Adjustment Quantity")
    quantity_after = forms.IntegerField(required=False, min_value=0, label="Counted Stock")
    reason_preset = forms.ChoiceField(choices=REASON_CHOICES, label="Reason")
    custom_reason = forms.CharField(required=False, max_length=150, label="Custom Reason")
    reason = forms.CharField(max_length=150, widget=forms.HiddenInput())
    notes = forms.CharField(required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["reason"].widget = forms.HiddenInput()
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("name")
        self.fields["adjustment_mode"].help_text = "Use Stock Take when you counted the shelf and want to set the final stock directly."
        self.fields["quantity_delta"].help_text = "Use positive numbers to add stock and negative numbers to remove stock."
        self.fields["quantity_after"].help_text = "Used only for stock take mode."
        self.fields["reason_preset"].help_text = "Pick the closest business reason so stock corrections stay consistent."
        self.fields["custom_reason"].help_text = "Use only when the preset list does not fit."
        self.fields["notes"].help_text = "Optional details like batch, shelf, staff note, or count reference."

        initial_reason = self.initial.get("reason") or getattr(self, "cleaned_data", {}).get("reason")
        if initial_reason:
            preset_values = {choice[0] for choice in self.REASON_CHOICES}
            if initial_reason in preset_values:
                self.fields["reason_preset"].initial = initial_reason
            else:
                self.fields["reason_preset"].initial = "other"
                self.fields["custom_reason"].initial = initial_reason

    def clean(self):
        cleaned_data = super().clean()
        mode = cleaned_data.get("adjustment_mode")
        reason_preset = cleaned_data.get("reason_preset")
        custom_reason = (cleaned_data.get("custom_reason") or "").strip()

        if mode == StockAdjustment.STOCK_TAKE:
            if cleaned_data.get("quantity_after") in (None, ""):
                self.add_error("quantity_after", "Enter the counted stock quantity.")
            cleaned_data["quantity_delta"] = None
            if reason_preset in (None, ""):
                cleaned_data["reason_preset"] = "stock_take_difference"
                reason_preset = "stock_take_difference"
        else:
            if cleaned_data.get("quantity_delta") in (None, ""):
                self.add_error("quantity_delta", "Enter the adjustment quantity.")
            cleaned_data["quantity_after"] = None

        if reason_preset == "other":
            if not custom_reason:
                self.add_error("custom_reason", "Enter the reason when Other is selected.")
            cleaned_data["reason"] = custom_reason
        else:
            reason_map = dict(self.REASON_CHOICES)
            cleaned_data["reason"] = reason_map.get(reason_preset, "")

        return cleaned_data




class SaleReturnForm(StyledFieldsMixin, forms.Form):
    refund_method = forms.ChoiceField(choices=SaleReturn.REFUND_METHOD_CHOICES)
    bank_account = forms.ModelChoiceField(queryset=bank_account_queryset(), required=False)
    reason = forms.CharField(max_length=150)
    notes = forms.CharField(required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["bank_account"].queryset = bank_account_queryset()
        self.fields["refund_method"].help_text = "Reduce receivable for credit sales, or refund from cash or a bank ledger."

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("refund_method") != SaleReturn.BANK:
            cleaned_data["bank_account"] = None
        elif not cleaned_data.get("bank_account"):
            self.add_error("bank_account", "Choose which bank ledger the refund is leaving from.")
        return cleaned_data


class PurchaseReturnForm(StyledFieldsMixin, forms.Form):
    settlement_method = forms.ChoiceField(choices=PurchaseReturn.SETTLEMENT_METHOD_CHOICES)
    bank_account = forms.ModelChoiceField(queryset=bank_account_queryset(), required=False)
    reason = forms.CharField(max_length=150)
    notes = forms.CharField(required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["bank_account"].queryset = bank_account_queryset()
        self.fields["settlement_method"].help_text = "Reduce payable when goods are returned against an unpaid supplier bill, or record a cash/bank refund."

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("settlement_method") != PurchaseReturn.BANK:
            cleaned_data["bank_account"] = None
        elif not cleaned_data.get("bank_account"):
            self.add_error("bank_account", "Choose which bank ledger received this supplier refund.")
        return cleaned_data
