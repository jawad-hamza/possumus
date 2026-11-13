from decimal import Decimal
from django import forms
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.db import transaction
from django.forms import BaseModelFormSet, modelformset_factory

from .models import (
    Brand,
    Category,
    CompanyProfile,
    CustomRole,
    Customer,
    OwnerOnboarding,
    Product,
    ProductGroup,
    UserPreference,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    SaleReturn,
    StockAdjustment,
    Supplier,
    SystemSettings,
    format_qty,
    quantize_money,
)
from .models import PACK_LEVELS
from accounts.receipt_forms import BankReceiptFormMixin
from accounts.services.cash_bank import bank_account_queryset
from .services.access import (
    ACCESS_CHOICES,
    CASHIER_ROLE,
    MODULE_DEFINITIONS,
    get_role_choices,
)
from .services.modules import allowed_unit_choices, locked_settings_flags, module_enabled, module_within_ceiling
from .services.user_timezones import normalize_time_zone_name, time_zone_choices

# Cartons and boxes on the product and stock adjustment forms.
from django.core.validators import DecimalValidator
from .models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_LEVEL_CHOICES,
    PACK_NAMES,
    PACK_PIECE,
    PACK_PLURALS,
    normalize_pack_level,
    packaging_enabled,
    quantize_qty,
)


DASHES = "---------"

# What an empty dropdown means, in the shop's words. Anything not named here gets "Choose a <label>"
# when a choice is required and "No <label>" when it may stay empty.
EMPTY_CHOICE_WORDS = {
    "brand": ("Choose a brand", "No brand"),
    "category": ("Choose a category", "No category"),
    "supplier": ("Choose a supplier", "Choose a supplier"),
    "customer": ("Choose a customer", "Walk-in customer"),
    "product": ("Choose a product", "No product"),
    "bank_account": ("Choose a bank account", "No bank account"),
}


def _empty_words(name, field):
    required_words, optional_words = EMPTY_CHOICE_WORDS.get(name, (None, None))
    if field.required and required_words:
        return required_words
    if not field.required and optional_words:
        return optional_words
    label = str(field.label or name.replace("_", " ")).strip().lower()
    if field.required:
        article = "an" if label[:1] in "aeiou" else "a"
        return f"Choose {article} {label}"
    return f"No {label}"


def plain_empty_choices(fields):
    """Replace Django's "---------" empty option with words that say what an empty choice means."""
    for name, field in fields.items():
        if isinstance(field, forms.ModelChoiceField) and not isinstance(field, forms.ModelMultipleChoiceField):
            if field.empty_label == DASHES:
                field.empty_label = _empty_words(name, field)
        elif isinstance(field, forms.ChoiceField) and not isinstance(field.widget, forms.HiddenInput):
            choices = list(field.choices)
            if any(value in ("", None) and label == DASHES for value, label in choices):
                words = _empty_words(name, field)
                field.choices = [(value, words if value in ("", None) and label == DASHES else label) for value, label in choices]


class StyledFieldsMixin:
    def _apply_bootstrap(self):
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


class StyledModelForm(StyledFieldsMixin, forms.ModelForm):
    pass


class LoginForm(StyledFieldsMixin, AuthenticationForm):
    username = forms.CharField(label="Email or login ID")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_verification_user = None
        self._apply_bootstrap()
        self.fields["username"].widget.attrs.setdefault("autofocus", True)
        self.fields["username"].widget.attrs.setdefault("placeholder", "Enter email or login ID")
        self.fields["password"].widget.attrs.setdefault("placeholder", "Enter password")

    @staticmethod
    def _find_user(identifier):
        identifier = (identifier or "").strip()
        if not identifier:
            return None
        user_model = get_user_model()
        user = user_model._default_manager.filter(username__iexact=identifier).first()
        if user is not None:
            return user
        return user_model._default_manager.filter(email__iexact=identifier).order_by("id").first()

    def clean(self):
        identifier = (self.cleaned_data.get("username") or "").strip()
        password = self.cleaned_data.get("password")
        self.pending_verification_user = None

        if identifier and password:
            matched_user = self._find_user(identifier)
            if matched_user is not None and not matched_user.is_active:
                # The auth backend rejects inactive users outright, so the password is
                # checked here. Only someone who proves the password learns that the
                # account is waiting for email verification; everyone else gets the
                # same generic error as for an unknown login.
                # Hashed unconditionally so every path costs one password check.
                password_proven = matched_user.check_password(password)
                onboarding = getattr(matched_user, "owner_onboarding", None)
                if password_proven and onboarding is not None and not onboarding.email_verified:
                    self.pending_verification_user = matched_user
                    raise forms.ValidationError(
                        "Verify your email before signing in. Check your inbox for the confirmation link.",
                        code="unverified",
                    )
                raise self.get_invalid_login_error()

            auth_username = matched_user.get_username() if matched_user is not None else identifier
            self.user_cache = authenticate(self.request, username=auth_username, password=password)
            if self.user_cache is None:
                raise self.get_invalid_login_error()
            self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data


class OwnerSignupForm(StyledFieldsMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=True)
    selected_plan = forms.ChoiceField(choices=OwnerOnboarding.PLAN_CHOICES, widget=forms.HiddenInput())

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ["first_name", "last_name", "email", "selected_plan"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["first_name"].widget.attrs.setdefault("placeholder", "Owner first name")
        self.fields["last_name"].widget.attrs.setdefault("placeholder", "Owner last name")
        self.fields["email"].widget.attrs.setdefault("placeholder", "owner@yourstore.com")
        self.fields["password1"].help_text = "Use at least 8 characters. A mix of letters, numbers, and symbols is best."
        self.fields["password2"].help_text = "Repeat the password so we can confirm it."

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            raise forms.ValidationError("Email is required.")

        # Deliberately no "already registered" error here: the signup view answers
        # every address the same way so the form cannot be used to probe for accounts.
        return email

    def clean_selected_plan(self):
        selected_plan = (self.cleaned_data.get("selected_plan") or OwnerOnboarding.PLAN_STARTER).strip().lower()
        allowed = {choice for choice, _ in OwnerOnboarding.PLAN_CHOICES}
        if selected_plan not in allowed:
            raise forms.ValidationError("Choose a valid pricing package before continuing.")
        return selected_plan

    def save(self, commit=True):
        user = super().save(commit=False)
        email = self.cleaned_data["email"]
        user.username = email
        user.email = email
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.is_active = False
        user.is_staff = False
        if commit:
            user.save()
        return user


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
        self.fields["username"].label = "Admin login ID"
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


class OwnerBusinessSetupForm(StyledModelForm):
    class Meta:
        model = CompanyProfile
        fields = ["name", "phone", "email", "address", "tax_registration_number"]
        labels = {
            "name": "Business name",
            "tax_registration_number": "Tax registration number",
        }
        help_texts = {
            "name": "This is the business name shown across the dashboard, invoices, and receipts.",
            "tax_registration_number": "Optional registration shown on receipts and tax documents.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["name"].widget.attrs.setdefault("autofocus", True)
        self.fields["name"].widget.attrs.setdefault("placeholder", "Your business name")
        self.fields["phone"].widget.attrs.setdefault("placeholder", "Business phone number")
        self.fields["email"].widget.attrs.setdefault("placeholder", "billing@yourstore.com")
        self.fields["address"].widget.attrs.setdefault("rows", 3)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Business name is required.")
        return name


class AdminTimeZoneForm(StyledModelForm):
    time_zone = forms.ChoiceField(choices=(), required=False, label="Time zone")

    class Meta:
        model = UserPreference
        fields = ["time_zone"]

    def __init__(self, *args, detected_time_zone="", effective_time_zone="", **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.detected_time_zone = normalize_time_zone_name(detected_time_zone, "")
        self.effective_time_zone = normalize_time_zone_name(effective_time_zone, "")
        self.fields["time_zone"].choices = time_zone_choices()
        if self.detected_time_zone:
            self.fields["time_zone"].help_text = (
                f"Leave this on System detected to follow the device time zone ({self.detected_time_zone})."
            )
        else:
            self.fields["time_zone"].help_text = (
                "Leave this on System detected to follow the device time zone when it is available."
            )

    def clean_time_zone(self):
        return normalize_time_zone_name(self.cleaned_data.get("time_zone"), "")


class TeamUserForm(StyledFieldsMixin, UserCreationForm):
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    role = forms.ChoiceField(choices=(), initial=CASHIER_ROLE)
    is_active = forms.BooleanField(required=False, initial=True)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ["username", "first_name", "last_name", "email", "role", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["username"].label = "Login ID"
        self.fields["username"].help_text = "Unique ID used by the team member to sign in."
        self.fields['role'].choices = get_role_choices()
        self.fields['role'].help_text = 'Choose Admin, Cashier, or a custom role created below. Custom roles follow the page permissions configured in Settings.'
        self.fields["password1"].help_text = "Use a strong password with at least 8 characters."

    # Sign-in accepts either a login ID or an email, so both must stay unambiguous
    # across every account (case-insensitively).
    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        user_model = get_user_model()
        if (
            user_model._default_manager.filter(username__iexact=username).exists()
            or user_model._default_manager.filter(email__iexact=username).exists()
        ):
            raise forms.ValidationError("That login ID is already in use. Choose a different one.")
        return username

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            return ""
        user_model = get_user_model()
        if (
            user_model._default_manager.filter(email__iexact=email).exists()
            or user_model._default_manager.filter(username__iexact=email).exists()
        ):
            raise forms.ValidationError("Another account already uses this email address.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        user.is_active = self.cleaned_data.get("is_active", True)
        if commit:
            user.save()
        return user


class AccountPasswordResetForm(StyledFieldsMixin, PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["email"].label = "Account email"
        self.fields["email"].widget.attrs.setdefault("autofocus", True)
        self.fields["email"].widget.attrs.setdefault("placeholder", "you@yourstore.com")


class AccountSetPasswordForm(StyledFieldsMixin, SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["new_password1"].widget.attrs.setdefault("autofocus", True)


class AccountPasswordChangeForm(StyledFieldsMixin, PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["old_password"].label = "Current password"


class CustomRoleForm(StyledModelForm):
    description = forms.CharField(required=False, max_length=255)

    class Meta:
        model = CustomRole
        fields = ["name", "description"]
        help_texts = {
            "name": "Example: Manager, Accountant, Floor Supervisor.",
            "description": "Optional note to explain what this role is meant to do.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        existing_permissions = getattr(self.instance, "permissions", {}) or {}
        for module in MODULE_DEFINITIONS:
            field_name = f"perm_{module['key']}"
            self.fields[field_name] = forms.ChoiceField(
                label=module["label"],
                choices=ACCESS_CHOICES,
                initial=existing_permissions.get(module["key"], "none"),
                help_text=module["description"],
                # An area added after a role was written (Distribution) reads as no access when not posted.
                required=False,
            )
            self.fields[field_name].widget.attrs.setdefault("class", "form-select")

    def clean_name(self):
        return (self.cleaned_data.get("name") or "").strip()

    def clean(self):
        cleaned_data = super().clean()
        permissions = {}
        for module in MODULE_DEFINITIONS:
            permissions[module["key"]] = cleaned_data.get(f"perm_{module['key']}") or "none"

        if not any(level != "none" for level in permissions.values()):
            raise forms.ValidationError("Give this role access to at least one page before saving it.")

        cleaned_data["permissions"] = permissions
        return cleaned_data

    def save(self, commit=True):
        role = super().save(commit=False)
        role.permissions = self.cleaned_data["permissions"]
        if commit:
            role.save()
        return role


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
            "name": "Business name",
            "tax_registration_number": "Tax registration number",
            "invoice_footer": "Receipt footer",
            "show_qr": "Show QR",
            "qr_note": "QR note",
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
        self.fields["logo"].widget.attrs.setdefault("accept", "image/*")
        limit_mb = self._logo_limit_bytes() / (1024 * 1024)
        self.fields["logo"].help_text = f"Image up to {limit_mb:g} MB. It appears on the sign-in page and receipts."
        if getattr(settings, "ON_VERCEL", False) and not getattr(settings, "USE_S3_MEDIA", False):
            self.fields["logo"].help_text += (
                " This hosting has no permanent disk, so the logo disappears after a redeploy until media storage is configured."
            )

    @staticmethod
    def _logo_limit_bytes():
        return int(getattr(settings, "MAX_LOGO_UPLOAD_BYTES", 2 * 1024 * 1024))

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        # Only a newly uploaded file has a content type; the stored logo is left alone.
        if logo and hasattr(logo, "content_type") and logo.size > self._logo_limit_bytes():
            limit_mb = self._logo_limit_bytes() / (1024 * 1024)
            raise forms.ValidationError(f"The logo is too large. Choose an image under {limit_mb:g} MB.")
        return logo


class SystemSettingsForm(StyledModelForm):
    class Meta:
        model = SystemSettings
        fields = [
            "gst_enabled",
            "tax_mode",
            "tax_label",
            "use_payment_method_gst",
            "cash_gst_rate",
            "bank_gst_rate",
            "enable_product_expiry",
            "expiry_alert_days",
            "allow_credit_sales",
            "credit_limit_action",
            "allow_price_override",
            "allow_line_discount",
            "allow_zero_total_sales",
            "allow_remove_free_goods",
            "default_sale_payment_method",
            "till_default_unit",
            "stock_default_unit",
            "require_bank_receipt",
            "thermal_printer_width_mm",
            "currency_symbol",
            "currency_code",
            "default_markup_percent",
            "invoice_prefix",
        ]
        labels = {
            "till_default_unit": "Sell by",
            "stock_default_unit": "Buy and count by",
            "gst_enabled": "Charge tax on sales",
            "tax_mode": "Tax mode",
            "tax_label": "Tax label",
            "use_payment_method_gst": "Different tax rate for cash and bank",
            "cash_gst_rate": "Cash tax rate (%)",
            "bank_gst_rate": "Bank tax rate (%)",
            "enable_product_expiry": "Enable product expiry tracking",
            "expiry_alert_days": "Expiry alert window (days)",
            "allow_credit_sales": "Allow credit sales",
            "credit_limit_action": "Credit sale over a customer's credit limit",
            "allow_price_override": "Allow price override",
            "allow_line_discount": "Allow line discount",
            "allow_zero_total_sales": "Allow zero-total sales",
            "allow_remove_free_goods": "Cashier may take free goods off a bill",
            "default_sale_payment_method": "Default payment method",
            "require_bank_receipt": "Require a receipt for bank payments",
            "thermal_printer_width_mm": "Thermal printer width (mm)",
            "currency_symbol": "Currency symbol",
            "currency_code": "Currency code",
            "default_markup_percent": "Default markup on cost (%)",
            "invoice_prefix": "Invoice number prefix",
        }
        help_texts = {
            "till_default_unit": "The unit a product goes on the bill in. The cashier can change it on the line.",
            "stock_default_unit": "The unit purchases start in and stock is shown from.",
            "tax_mode": "Category tax applies each product line's rate from its category. Manual cart tax lets the cashier type one rate for the whole sale.",
            "tax_label": "Examples: GST, VAT, Sales Tax.",
            "use_payment_method_gst": "When enabled, Cash and Bank sales use the two rates below and the POS tax box becomes automatic.",
            "cash_gst_rate": "Applied when the sale is completed as a cash sale.",
            "bank_gst_rate": "Applied when the sale is completed as a bank sale.",
            "enable_product_expiry": "Show expiry date fields across products and purchases.",
            "credit_limit_action": "Refuse stops the sale at the till until a payment is taken or the limit is raised. Warn only lets the sale through and shows a warning. Customers without a credit limit are never stopped.",
            "allow_zero_total_sales": "Off by default. When on, a sale discounted to zero (a giveaway) can be completed; the stock still leaves and its cost is recorded.",
            "expiry_alert_days": "How many days before expiry the dashboard should start warning you.",
            "thermal_printer_width_mm": "50mm is standard for small thermal receipt printers.",
            "currency_symbol": "Shown before every amount on screens, receipts and reports.",
            "currency_code": "Three-letter ISO code of the currency you trade in.",
            "default_markup_percent": "Pre-filled on new products so the sale price is cost plus this %. Leave blank to price by hand.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["use_payment_method_gst"].required = False
        self.fields["cash_gst_rate"].required = False
        self.fields["bank_gst_rate"].required = False
        self.fields["enable_product_expiry"].required = False
        self.fields["credit_limit_action"].required = False
        self.fields["expiry_alert_days"].required = False
        # Optional so a settings POST that predates these fields keeps the stored values.
        self.fields["currency_symbol"].required = False
        self.fields["currency_code"].required = False
        self.fields["default_markup_percent"].required = False
        self.fields["default_markup_percent"].widget.attrs.update({"min": "0", "step": "0.01"})
        self.fields["currency_code"].widget.attrs.update({"maxlength": "3", "style": "text-transform: uppercase;"})
        for name in ("till_default_unit", "stock_default_unit"):
            self.fields[name].required = False
            # A plain choice: the unit is never "none".
            self.fields[name].choices = [choice for choice in self.fields[name].choices if choice[0]]
        # Switches of modules the shop cannot use are read-only: a disabled field ignores the POST,
        # so nobody can turn one on by hand-crafting a request.
        self.locked_flags = locked_settings_flags()
        for flag in self.locked_flags:
            if flag in self.fields:
                self.fields[flag].disabled = True
        if not module_enabled("markup"):
            self.fields["default_markup_percent"].disabled = True
        if not module_enabled("schemes"):
            # Not on the page while schemes are off: a disabled field keeps what is stored.
            self.fields["allow_remove_free_goods"].disabled = True

    def clean_credit_limit_action(self):
        # A settings POST that predates this choice keeps the stored one.
        return self.cleaned_data.get("credit_limit_action") or self.instance.credit_limit_action

    def clean_tax_label(self):
        return (self.cleaned_data.get("tax_label") or "GST").strip() or "GST"

    def clean_currency_symbol(self):
        symbol = (self.cleaned_data.get("currency_symbol") or "").strip()
        if not symbol and "currency_symbol" not in self.data:
            return self.instance.currency_symbol
        return symbol

    def clean_currency_code(self):
        code = (self.cleaned_data.get("currency_code") or "").strip().upper()
        if not code:
            return self.instance.currency_code
        if len(code) != 3 or not code.isalpha():
            raise forms.ValidationError("Use the three-letter currency code, for example USD or AED.")
        return code

    def clean_default_markup_percent(self):
        markup = self.cleaned_data.get("default_markup_percent")
        if markup is None and "default_markup_percent" not in self.data:
            return self.instance.default_markup_percent
        if markup is not None and markup < 0:
            raise forms.ValidationError("Markup cannot be negative.")
        return markup

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("cash_gst_rate") in (None, ""):
            cleaned_data["cash_gst_rate"] = self.instance.cash_gst_rate if self.instance.pk else Decimal("0")
        if cleaned_data.get("bank_gst_rate") in (None, ""):
            cleaned_data["bank_gst_rate"] = self.instance.bank_gst_rate if self.instance.pk else Decimal("0")
        if cleaned_data.get("expiry_alert_days") in (None, ""):
            cleaned_data["expiry_alert_days"] = self.instance.expiry_alert_days if self.instance.pk else 30
        # A settings POST that leaves the unit choices out keeps what is stored.
        for name in ("till_default_unit", "stock_default_unit"):
            if not cleaned_data.get(name):
                cleaned_data[name] = getattr(self.instance, name)
        return cleaned_data


# City, route, order booker and credit limit behave the same on every customer form.
from .customer_form_mixins import CustomerTermsMixin, PartyCityMixin  # noqa: E402


class CustomerForm(CustomerTermsMixin, StyledModelForm):
    class Meta:
        model = Customer
        fields = ["name", "phone", "email", "address", "city", "route", "order_booker", "credit_limit"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class SupplierForm(PartyCityMixin, StyledModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "phone", "email", "address", "city"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


def _existing_model_fields(model, names):
    available = {field.name for field in model._meta.get_fields()}
    return [name for name in names if name in available]


# Services and other non-stock items: the switch appears once Product has a track_stock column.
PRODUCT_STOCK_SWITCH_FIELDS = _existing_model_fields(Product, ("track_stock",))


def _apply_feature_modules_to_product_form(form):
    """Product forms shrink with the shop's feature modules (pos.services.modules)."""
    if "unit" in form.fields:
        # Whole-number units only while Weights & Measures is off; the choices also validate the POST.
        form.fields["unit"].choices = allowed_unit_choices(getattr(form.instance, "unit", None) if form.instance.pk else None)
        if module_enabled("packaging"):
            # How it is sold (carton, box or piece) is picked on each line; this is only the smallest unit.
            form.fields["unit"].label = "Counted in"
            form.fields["unit"].help_text = "The smallest unit: a piece, or kg / litre for loose goods. Cartons and boxes go under Packing."
    if not module_enabled("markup"):
        # Only "type the price" is left: no chooser, no markup inputs.
        for name in ("price_by", "markup_percent", "markup_amount"):
            form.fields.pop(name, None)
    # Expiry dates stay editable while tracking is merely switched off (only the alerts go quiet);
    # the field disappears when the edition does not include expiry tracking at all.
    if not module_within_ceiling("expiry"):
        form.fields.pop("expiry_date", None)
    if not packaging_enabled():
        # No cartons or boxes: the packing numbers and the unit pickers go, quantities are plain again.
        for name in (*PACKING_FIELDS, "opening_stock_unit", "low_stock_unit"):
            form.fields.pop(name, None)
    if not module_enabled("price_lists") and "retail_price" in form.fields:
        # The retail price (MRP) belongs to the trade price lists module; a stored one stays as it is.
        form.fields.pop("retail_price")
    elif "retail_price" in form.fields:
        form.fields["retail_price"].required = False
        form.fields["retail_price"].widget.attrs.update({"min": "0"})


# ------------------------------------------------------------------ cartons, boxes, pieces
# Typed per the largest pack the product has (the carton when there is one); stored per base unit.
PACK_MONEY_FIELDS = ("cost_price", "price", "markup_amount", "retail_price")
PACK_MONEY_WORDS = {
    "cost_price": "Cost",
    "price": "Sale price",
    "markup_amount": "Amount on cost",
    "retail_price": "Retail price (MRP)",
}
PACK_MONEY_HELP = {
    "cost_price": "What one carton costs you, or the largest pack the product has. Box and piece follow from the packing.",
    "price": "Price of one carton at the till, or of the largest pack. Box and piece follow from the packing.",
    "markup_amount": "Sale price = cost + this amount, for the same pack as the cost.",
    "retail_price": "The retail price printed on one carton, or the largest pack. Shown on invoices and load sheets, never charged.",
}
PACKING_FIELDS = ("boxes_per_carton", "pieces_per_box")
PACK_UNIT_WORDS = {PACK_CARTON: "Cartons", PACK_BOX: "Boxes"}
# The stock columns hold nine whole digits.
MAX_BASE_QUANTITY = Decimal("999999999")


def pack_unit_choices(piece_unit=None):
    """Cartons / Boxes / the product's own unit, for a quantity that is typed with its unit."""
    piece_unit = piece_unit or Product.UNIT_PIECE
    piece_word = "Pieces" if piece_unit == Product.UNIT_PIECE else piece_unit
    return [(PACK_CARTON, PACK_UNIT_WORDS[PACK_CARTON]), (PACK_BOX, PACK_UNIT_WORDS[PACK_BOX]), (PACK_PIECE, piece_word)]


def default_stock_unit():
    """The unit stock is shown from and entry starts in (Settings): the carton unless the shop changed it."""
    return normalize_pack_level(SystemSettings.load().stock_default_unit, default=PACK_CARTON)


def quantity_in_largest_unit(product, quantity, default_level=None):
    """(value, level): a base quantity in the largest of the product's units that divides it exactly."""
    quantity = quantize_qty(quantity)
    if quantity <= 0:
        return quantity, product.resolve_pack_level(default_level or PACK_PIECE)
    for level in product.pack_levels:
        if level["factor"] > 1 and quantity % level["factor"] == 0:
            return quantity / level["factor"], level["code"]
    return quantity, PACK_PIECE


def typed_quantity_to_base(form, product, name, quantity, level):
    """A quantity typed with its unit, in base units. None once the reason it cannot be is on the form."""
    if level != PACK_PIECE:
        if level not in product.pack_level_codes:
            form.add_error(name, f"This product has no {PACK_PLURALS[level]}. Choose another unit.")
            return None
        if quantity != quantity.to_integral_value():
            form.add_error(name, f"{PACK_PLURALS[level].capitalize()} are counted in whole numbers.")
            return None
    base = product.to_base_quantity(quantity, level)
    if abs(base) > MAX_BASE_QUANTITY:
        form.add_error(name, "That is more than the stock count can hold.")
        return None
    return base


def _as_plain_money(field):
    """One piece is priced to six places in the database; on a form money is typed to two."""
    field.max_digits, field.decimal_places = 12, 2
    field.validators = [check for check in field.validators if not isinstance(check, DecimalValidator)]
    field.validators.append(DecimalValidator(12, 2))
    field.widget.attrs["step"] = "0.01"


class ProductForm(StyledModelForm):
    # Stock is never edited here: a stale form would overwrite sales made meanwhile.
    # New products take an opening quantity; later changes go through Stock Adjustments.
    opening_stock = forms.DecimalField(
        label="Opening stock",
        required=False,
        min_value=0,
        max_digits=12,
        decimal_places=3,
        help_text="What is on the shelf today.",
    )
    # Opening stock and the alert level are typed with their unit (cartons first) and kept in base units.
    opening_stock_unit = forms.ChoiceField(label="Opening stock unit", choices=PACK_LEVEL_CHOICES, required=False)
    low_stock_unit = forms.ChoiceField(label="Low stock alert unit", choices=PACK_LEVEL_CHOICES, required=False)
    # A select, not a checkbox: a POST without the field must mean "keep counting stock",
    # and an unticked checkbox cannot be told apart from a missing one.
    track_stock = forms.TypedChoiceField(
        label="Item type",
        choices=(("1", "Stocked item"), ("0", "Service (no stock)")),
        coerce=lambda value: value == "1",
        required=False,
        empty_value=None,
        help_text="Service: labour, delivery, repairs.",
    )
    PRICE_BY_CHOICES = (
        (Product.PRICE_BY_HAND, "Type the price"),
        (Product.PRICE_BY_PERCENT, "Percentage on cost"),
        (Product.PRICE_BY_AMOUNT, "Amount on cost"),
    )
    price_by = forms.ChoiceField(
        label="Price by",
        choices=PRICE_BY_CHOICES,
        required=False,
        help_text="Type the sale price yourself, or let it follow the cost: cost + 25%, or cost + 20.",
    )
    # Asked once, when a second size turns a plain product into a family of sizes.
    source_variant_label = forms.CharField(
        label="Size of the first one",
        max_length=40,
        required=False,
        help_text="What the item you started from is called, such as Small.",
    )

    class Meta:
        model = Product
        fields = [
            "name",
            "variant_label",
            "brand",
            "sku",
            "barcode",
            "category",
            "supplier",
            "unit",
            *PACKING_FIELDS,
            "cost_price",
            "markup_percent",
            "markup_amount",
            "price",
            "minimum_margin_percent",
            "retail_price",
            "low_stock_threshold",
            "location",
            "expiry_date",
            "is_active",
            *PRODUCT_STOCK_SWITCH_FIELDS,
        ]
        labels = {
            "retail_price": "Retail price (MRP)",
            "sku": "SKU",
            "unit": "Sold by",
            "boxes_per_carton": "Boxes in a carton",
            "pieces_per_box": "Pieces in a box",
            "price": "Sale price",
            "cost_price": "Current cost",
            "markup_percent": "Markup on cost (%)",
            "markup_amount": "Amount on cost",
            "variant_label": "Size",
            "minimum_margin_percent": "Minimum margin (%)",
            "location": "Storage location",
            "expiry_date": "Expiry date",
        }
        help_texts = {
            "barcode": "Optional. The SKU scans too.",
            "unit": "Loose goods: kg, g, L or m.",
            "cost_price": "What one unit costs you.",
            "markup_percent": "Sale price = cost + this %.",
            "markup_amount": "Sale price = cost + this amount.",
            "variant_label": "Such as Small, 500 g or Pack of 6.",
            "price": "Price per unit at the till.",
            "minimum_margin_percent": "No sale price goes below it.",
            "location": "Such as Rack 3 or Shelf A.",
            "expiry_date": "Leave blank if it does not expire.",
            "retail_price": "The price printed on the pack. Shown on invoices, never charged.",
        }

    def clean_retail_price(self):
        retail = self.cleaned_data.get("retail_price")
        if retail is not None and retail < 0:
            raise forms.ValidationError("The retail price cannot be negative. Leave it empty when there is none.")
        return retail

    def __init__(self, *args, variant_source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["price"].required = False
        self.fields["unit"].required = False
        self.initial["track_stock"] = "1" if self.instance.track_stock else "0"
        self.initial["price_by"] = self.instance.price_by
        if self.instance.pk:
            del self.fields["opening_stock"]
        elif module_enabled("markup"):
            if self.initial.get("markup_amount") is not None:
                self.initial["price_by"] = Product.PRICE_BY_AMOUNT
            elif self.initial.get("markup_percent") is not None:
                self.initial["price_by"] = Product.PRICE_BY_PERCENT
            else:
                default_markup = SystemSettings.load().default_markup_percent
                if default_markup is not None:
                    self.fields["markup_percent"].initial = default_markup
                    self.initial["price_by"] = Product.PRICE_BY_PERCENT
        self._init_packing()
        self.fields["markup_amount"].widget.attrs.update({"min": "0", "step": "0.01"})

        # Sizes: the label shows on a product that belongs to a family, and on a size being added.
        self.variant_source = variant_source if (variant_source is not None and module_enabled("variants")) else None
        grouped = bool(self.instance.pk and self.instance.variant_group_id)
        if self.variant_source is not None:
            self.fields["variant_label"].required = True
            self.fields["variant_label"].label = "This size"
            if self.variant_source.variant_group_id:
                del self.fields["source_variant_label"]
            else:
                self.fields["source_variant_label"].required = True
                self.fields["source_variant_label"].label = f"Size of the first one ({self.variant_source.sku})"
        else:
            del self.fields["source_variant_label"]
            if grouped and module_enabled("variants"):
                self.fields["variant_label"].required = True
            else:
                del self.fields["variant_label"]
        if self.variant_source is not None or grouped:
            self.fields["name"].help_text = "Shared by every size of this item."
        self._apply_bootstrap()
        self.fields["brand"].queryset = Brand.objects.filter(is_active=True).order_by("name")
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["supplier"].queryset = Supplier.objects.filter(is_active=True).order_by("name")
        self.fields["expiry_date"].required = False
        self.fields["expiry_date"].widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})
        _apply_feature_modules_to_product_form(self)

    def _init_packing(self):
        """Cost, price and the fixed markup read per the largest pack; quantities read with their unit."""
        product = self.instance
        self.packaging = packaging_enabled()
        self.default_stock_unit = default_stock_unit() if self.packaging else PACK_PIECE
        for name in PACKING_FIELDS:
            self.fields[name].required = False
            self.fields[name].widget.attrs.update({"min": "1", "step": "1", "inputmode": "numeric"})

        # What is stored for one base unit, read before clean() starts moving values onto the instance.
        self._stored_pack_money = {name: getattr(product, name) for name in PACK_MONEY_FIELDS} if product.pk else {}
        top = product.top_pack_level
        top_factor = product.pack_factor(top)
        for name in PACK_MONEY_FIELDS:
            field = self.fields[name]
            _as_plain_money(field)
            if self.packaging:
                # The labels follow the packing numbers in the browser: "Cost per carton".
                field.widget.attrs.update({"data-plain-label": field.label, "data-pack-label": PACK_MONEY_WORDS[name]})
                field.help_text = PACK_MONEY_HELP[name]
                if product.has_packaging:
                    field.label = f"{PACK_MONEY_WORDS[name]} per {PACK_NAMES[top].lower()}"
            stored = self._stored_pack_money.get(name)
            if stored is not None:
                # Shown multiplied back up to the pack; an untouched figure is never divided again.
                self.initial[name] = quantize_money(Decimal(stored) * top_factor)
                field.widget.attrs["data-base"] = format(Decimal(stored), "f")

        unit_choices = pack_unit_choices(product.unit if product.pk else self.initial.get("unit"))
        for name, label in (("opening_stock_unit", "Opening stock unit"), ("low_stock_unit", "Low stock alert unit")):
            self.fields[name].choices = unit_choices
            self.fields[name].widget.attrs["aria-label"] = label
        if product.pk:
            del self.fields["opening_stock_unit"]
            # The alert level reads in the largest unit that divides it exactly: 720 pieces is 5 cartons.
            value, level = quantity_in_largest_unit(product, product.low_stock_threshold, self.default_stock_unit)
            self.initial["low_stock_threshold"] = format_qty(value)
            self.initial["low_stock_unit"] = level
            # What the page showed, to tell an untouched alert level from a typed one.
            self._shown_alert_level = (quantize_qty(value), level)
        else:
            # A new product starts in the shop's stock unit as soon as its packing gives that level.
            packing = Product(
                pieces_per_box=self.initial.get("pieces_per_box") or 1,
                boxes_per_carton=self.initial.get("boxes_per_carton") or 1,
            )
            start_level = packing.resolve_pack_level(self.default_stock_unit)
            self.initial.setdefault("opening_stock_unit", start_level)
            self.initial.setdefault("low_stock_unit", start_level)

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("unit"):
            cleaned["unit"] = self.instance.unit or Product.UNIT_PIECE
            self.instance.unit = cleaned["unit"]
        self._clean_packing(cleaned)
        if "markup_percent" not in self.fields:
            # Markup Pricing is off: the typed price is the price, so a stored markup must not overrule it.
            self.instance.markup_percent = None
            self.instance.markup_amount = None
        self._clean_price_by(cleaned)
        # Everything here is still as typed: per the largest pack. _store_pack_money divides it at the end.
        markup = cleaned.get("markup_percent")
        markup_amount = cleaned.get("markup_amount")
        cost = cleaned.get("cost_price")
        price_follows_markup = bool(cost) and (markup is not None or markup_amount is not None)
        if markup is not None and cost:
            cleaned["price"] = quantize_money(cost * (Decimal("1") + markup / Decimal("100")))
            self.instance.price = cleaned["price"]
        elif markup_amount is not None and cost:
            cleaned["price"] = quantize_money(cost + markup_amount)
            self.instance.price = cleaned["price"]
        elif not cleaned.get("price"):
            self.add_error("price", "Enter a sale price, or set a cost and markup to calculate it.")
        self._clean_variant(cleaned)

        track_stock = cleaned.get("track_stock")
        if track_stock is None:
            track_stock = cleaned["track_stock"] = self.instance.track_stock
        if not track_stock:
            if cleaned.get("opening_stock"):
                self.add_error("opening_stock", "A service has no stock. Leave this empty, or choose Stocked item.")
            if self.instance.pk and Product.objects.filter(pk=self.instance.pk, track_stock=True, stock__gt=0).exists():
                self.add_error(
                    "track_stock",
                    "This item still has stock on the books. Bring it to zero with a stock adjustment first.",
                )

        self._clean_stock_quantities(cleaned)
        self._store_pack_money(cleaned, price_follows_markup)
        return cleaned

    def _clean_packing(self, cleaned):
        """Put the packing on the instance first: everything typed per pack is read against it."""
        for name in PACKING_FIELDS:
            if name not in self.fields or name in self.errors:
                continue
            value = cleaned.get(name)
            if value is None:
                # Left out of the POST keeps what the product has; an emptied box means the level is not there.
                omitted = self.fields[name].widget.value_omitted_from_data(self.data, self.files, self.add_prefix(name))
                value = cleaned[name] = getattr(self.instance, name) if omitted else 1
            setattr(self.instance, name, value)

    def _clean_stock_quantities(self, cleaned):
        """Opening stock and the alert level are typed with a unit and kept in base units."""
        product = self.instance
        for name, unit_name in (("opening_stock", "opening_stock_unit"), ("low_stock_threshold", "low_stock_unit")):
            quantity = cleaned.get(name)
            if name not in self.fields or quantity is None:
                continue
            level = normalize_pack_level(cleaned.get(unit_name)) if unit_name in self.fields else PACK_PIECE
            if name == "low_stock_threshold" and product.pk and self._alert_level_untouched(quantity, cleaned.get(unit_name)):
                # Untouched: the level stays the same number of pieces whatever happened to the packing.
                cleaned[name] = product.low_stock_threshold
                continue
            if (
                name == "opening_stock"
                and level == PACK_PIECE
                and cleaned.get("unit") not in Product.FRACTIONAL_UNITS
                and quantity != quantity.to_integral_value()
            ):
                self.add_error(name, "This unit is counted in whole numbers.")
                continue
            base = typed_quantity_to_base(self, product, name, quantity, level)
            if base is not None:
                cleaned[name] = base

    def _alert_level_untouched(self, quantity, posted_unit):
        """The alert level came back as the page showed it (a POST without the unit counts as the same unit)."""
        shown = getattr(self, "_shown_alert_level", None)
        if shown is None:
            return False
        shown_quantity, shown_level = shown
        if quantize_qty(quantity) != shown_quantity:
            return False
        return "low_stock_unit" not in self.fields or not posted_unit or normalize_pack_level(posted_unit) == shown_level

    def _store_pack_money(self, cleaned, price_follows_markup=False):
        """Cost, price and the fixed markup arrive per the largest pack and are kept per base unit.

        A figure the user left alone keeps its stored value exactly: dividing what was shown
        (two places, multiplied up) would move it a little on every save.
        """
        product = self.instance
        factor = product.pack_factor(product.top_pack_level)
        # The page lists the figures the user typed over by hand, so "1,000 a carton" typed again
        # for a carton that changed size is taken as said. Without the page, changed_data decides.
        typed_by_hand = set((self.data.get(self.add_prefix("pack_money_typed")) or "").split(","))
        for name in PACK_MONEY_FIELDS:
            typed = cleaned.get(name)
            if typed is None:
                continue
            stored = self._stored_pack_money.get(name)
            untouched = stored is not None and (
                # Exactly what the stored figure reads as at this packing: nothing to change.
                quantize_money(Decimal(stored) * factor) == quantize_money(typed)
                or (name not in self.changed_data and name not in typed_by_hand)
            )
            if untouched and not (name == "price" and price_follows_markup):
                cleaned[name] = stored
            else:
                cleaned[name] = product.quantize_unit_money(Decimal(typed) / factor)
            setattr(product, name, cleaned[name])

    def _clean_price_by(self, cleaned):
        """One way of pricing at a time. Without the chooser in the POST the filled markup decides."""
        if "price_by" not in self.fields:
            return
        price_by = cleaned.get("price_by") or ""
        if not price_by:
            if cleaned.get("markup_percent") is not None and cleaned.get("markup_amount") is not None:
                self.add_error("markup_amount", "Use a percentage or an amount on cost, not both.")
            return
        if price_by == Product.PRICE_BY_PERCENT:
            cleaned["markup_amount"] = None
            if cleaned.get("markup_percent") is None and "markup_percent" not in self.errors:
                self.add_error("markup_percent", "Enter the percentage to add on cost.")
        elif price_by == Product.PRICE_BY_AMOUNT:
            cleaned["markup_percent"] = None
            if cleaned.get("markup_amount") is None and "markup_amount" not in self.errors:
                self.add_error("markup_amount", "Enter the amount to add on cost.")
        else:
            cleaned["markup_percent"] = None
            cleaned["markup_amount"] = None
        # construct_instance skips nothing here, but keep the instance in step for Product.clean().
        self.instance.markup_percent = cleaned.get("markup_percent")
        self.instance.markup_amount = cleaned.get("markup_amount")

    def _clean_variant(self, cleaned):
        source = self.variant_source
        if source is None:
            return
        label = (cleaned.get("variant_label") or "").strip()
        if source.variant_group_id:
            # Product.clean() checks the label against the sizes this family already has.
            self.instance.variant_group_id = source.variant_group_id
            return
        source_label = (cleaned.get("source_variant_label") or "").strip()
        if label and source_label and label.lower() == source_label.lower():
            self.add_error("variant_label", "Give the two sizes different names.")

    def save(self, commit=True):
        source = self.variant_source
        if not commit:
            return super().save(commit=False)
        with transaction.atomic():
            if source is not None:
                group = source.variant_group
                if group is None:
                    group = ProductGroup.objects.create(
                        name=self.cleaned_data["name"], category=source.category, brand=source.brand
                    )
                    source.variant_group = group
                    source.variant_label = self.cleaned_data["source_variant_label"].strip()
                    source.name = group.name
                    source.save(update_fields=["variant_group", "variant_label", "name"])
                self.instance.variant_group = group
            product = super().save(commit=True)
            group = product.variant_group
            if group is not None and group.name != product.name:
                # The name is the family's: renaming one size renames them all.
                group.name = product.name
                group.save()
        return product


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
            "unit",
            *PACKING_FIELDS,
            "markup_percent",
            "markup_amount",
            "price",
            "minimum_margin_percent",
            "low_stock_threshold",
            "location",
            "expiry_date",
        ]
        labels = {
            "sku": "SKU",
            "unit": "Sold by",
            "boxes_per_carton": "Boxes in a carton",
            "pieces_per_box": "Pieces in a box",
            "markup_percent": "Markup on cost (%)",
            "markup_amount": "Or amount on cost",
            "price": "Opening sale price",
            "minimum_margin_percent": "Minimum margin (%)",
            "low_stock_threshold": "Low stock alert",
            "expiry_date": "Expiry date",
        }
        help_texts = {
            "unit": "Pick kg, g, L or m for items sold by weight or length.",
            "markup_percent": "Fill this and the purchase line prices the item as cost + this %.",
            "markup_amount": "Or fill this: the purchase line prices the item as cost + this amount.",
            "price": "Use the intended retail price. Leave blank when a markup is set.",
            "barcode": "Optional. Leave blank if you only use SKU.",
            "expiry_date": "Optional. Leave blank for non-expiring products.",
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
        self.fields["expiry_date"].required = False
        self.fields["minimum_margin_percent"].initial = 0
        self.fields["low_stock_threshold"].initial = 5
        self.fields["expiry_date"].widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})
        self.fields["unit"].required = False
        self.fields["price"].required = False
        if module_enabled("markup"):
            default_markup = SystemSettings.load().default_markup_percent
            if default_markup is not None:
                self.fields["markup_percent"].initial = default_markup
        _apply_feature_modules_to_product_form(self)
        self._init_packing()

    def _init_packing(self):
        """Money is typed to two places; with cartons and boxes on it is typed for the largest pack."""
        for name in ("price", "markup_amount"):
            if name in self.fields:
                _as_plain_money(self.fields[name])
        if "pieces_per_box" not in self.fields:
            # The Cartons and boxes module is off: the packing numbers were taken out above.
            return
        for name in PACKING_FIELDS:
            self.fields[name].required = False
            self.fields[name].widget.attrs.update({"min": "1", "step": "1", "inputmode": "numeric"})
        self.fields["price"].help_text = (
            "The retail price of one carton, or of the largest pack the product has. Leave blank when a markup is set."
        )
        if "markup_amount" in self.fields:
            self.fields["markup_amount"].help_text = (
                "Or fill this: the purchase line prices the item as cost + this amount, "
                "for one carton or the largest pack the product has."
            )

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("unit"):
            cleaned["unit"] = Product.UNIT_PIECE
            self.instance.unit = cleaned["unit"]
        if cleaned.get("markup_percent") is not None and cleaned.get("markup_amount") is not None:
            self.add_error("markup_amount", "Use a percentage or an amount on cost, not both.")
        if cleaned.get("price") in (None, ""):
            if cleaned.get("markup_percent") is None and cleaned.get("markup_amount") is None:
                if "markup_percent" in self.fields:
                    self.add_error("price", "Enter a sale price, or set a markup so purchases can price it.")
                else:
                    self.add_error("price", "Enter a sale price.")
            else:
                # No cost yet: the first purchase line sets the price from the markup.
                cleaned["price"] = Decimal("0")
                self.instance.price = cleaned["price"]
        self._store_pack_money(cleaned)
        return cleaned

    def _store_pack_money(self, cleaned):
        """The opening price and a fixed markup arrive for the largest pack and are kept for one base unit."""
        if "pieces_per_box" not in self.fields or any(name in self.errors for name in PACKING_FIELDS):
            return
        product = self.instance
        for name in PACKING_FIELDS:
            # An emptied box means the product has no such level.
            cleaned[name] = cleaned.get(name) or 1
            setattr(product, name, cleaned[name])
        product.unit = cleaned["unit"]
        factor = product.pack_factor(product.top_pack_level)
        for name in ("price", "markup_amount"):
            if cleaned.get(name):
                cleaned[name] = product.quantize_unit_money(Decimal(cleaned[name]) / factor)

    def save(self, commit=True):
        product = super().save(commit=False)
        product.stock = 0
        product.cost_price = 0
        product.is_active = True
        if commit:
            product.save()
        return product


# The supplier's own bill reference: shown on the purchase form as soon as the Purchase model carries these columns.
PURCHASE_BILL_FIELDS = _existing_model_fields(Purchase, ("supplier_invoice_number", "bill_date"))


class PurchaseForm(BankReceiptFormMixin, StyledModelForm):
    # The amount paid now through a bank carries its receipt (see accounts.receipt_forms).
    bank_receipt_amount_field = "paid_amount"

    class Meta:
        model = Purchase
        fields = ["supplier", *PURCHASE_BILL_FIELDS, "payment_method", "bank_account", "paid_amount"]
        labels = {
            "supplier_invoice_number": "Supplier bill no.",
            "bill_date": "Bill date",
            "supplier": "Supplier",
            "payment_method": "Payment method",
            "bank_account": "Bank ledger",
            "paid_amount": "Paid now",
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
        for name in PURCHASE_BILL_FIELDS:
            self.fields[name].required = False
        if "bill_date" in self.fields:
            self.fields["bill_date"].widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})

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


def purchase_option_attrs(product, packaging=None):
    """The data-* attributes of a product's option on a purchase line, read by purchase.js.

    The price and a fixed markup amount are for one base unit (a piece): the script multiplies
    them by the size of the unit the line is in. Packing is only given while the Cartons and
    boxes module is on, so without it every product reads as plain.
    """
    if packaging is None:
        packaging = packaging_enabled()
    attrs = {
        "data-unit": product.unit,
        "data-step": product.quantity_step,
        "data-markup": "" if product.markup_percent is None else str(product.markup_percent),
        "data-markup-amount": (
            "" if product.markup_amount is None else str(product.quantize_unit_money(product.markup_amount))
        ),
        "data-min-margin": str(product.minimum_margin_percent or 0),
        "data-price": str(product.quantize_unit_money(product.price)) if product.price else "",
    }
    if packaging:
        attrs["data-pieces-per-box"] = str(product.pieces_per_box or 1)
        attrs["data-boxes-per-carton"] = str(product.boxes_per_carton or 1)
    return attrs


class PurchaseProductSelect(forms.Select):
    """Product dropdown whose options carry unit, packing and pricing rules for the purchase line script."""

    def optgroups(self, name, value, attrs=None):
        # Asked once for the dropdown, not once for every product in it.
        self._packaging = packaging_enabled()
        return super().optgroups(name, value, attrs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        product = getattr(value, "instance", None)
        if product is not None:
            option["attrs"].update(purchase_option_attrs(product, getattr(self, "_packaging", None)))
        return option


# A carton of 9,999 boxes of 9,999 pieces still fits the unit_factor column.
LINE_PACKING_MAX = 9999
# A blank purchase row already posts these, so alone they do not make a line.
PURCHASE_LINE_UNIT_FIELDS = ("unit_level", *PACKING_FIELDS)


class PurchaseItemForm(StyledModelForm):
    # The chosen product's packing, shown on the line and editable there. These are not columns
    # of the line: saving the purchase writes them to the product (views.purchases._save_purchase_lines).
    boxes_per_carton = forms.IntegerField(
        label="Boxes in a carton", required=False, min_value=1, max_value=LINE_PACKING_MAX
    )
    pieces_per_box = forms.IntegerField(
        label="Pieces in a box", required=False, min_value=1, max_value=LINE_PACKING_MAX
    )

    class Meta:
        model = PurchaseItem
        fields = ["product", "quantity", "unit_level", "cost_price", "sale_price"]
        labels = {
            "unit_level": "Unit",
            "cost_price": "Unit cost",
            "sale_price": "Sale price",
        }
        help_texts = {
            "sale_price": "The purchase will update the product's active sale price.",
        }
        widgets = {
            "product": PurchaseProductSelect,
        }

    def __init__(self, *args, default_unit=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.empty_permitted = True
        # (pieces in a box, boxes in a carton) once clean() finds the line changed its product's packing.
        self.packing_change = None
        # Each size is its own choice, named "Bread - Small".
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("name", "variant_label")
        self.fields["quantity"].widget.attrs.update({"min": "0", "inputmode": "decimal"})
        self.fields["sale_price"].required = False
        saved = self.instance if self.instance.pk and self.instance.product_id else None
        # What a line saved earlier holds. The browser keeps that unit and its size on offer until
        # the line's product, unit or packing is changed.
        self.saved_line = None
        if saved is not None:
            level = normalize_pack_level(saved.unit_level)
            # New rows get their step from the chosen option in the browser.
            self.fields["quantity"].widget.attrs["step"] = saved.product.quantity_step if level == PACK_PIECE else "1"
            self.initial["quantity"] = format_qty(saved.quantity)
            self.saved_line = {
                "product_id": saved.product_id,
                "level": level,
                "label": saved.unit_label,
                "factor": format_qty(saved.unit_factor),
                "pieces_per_box": saved.product.pieces_per_box,
                "boxes_per_carton": saved.product.boxes_per_carton,
            }
        if packaging_enabled():
            self._init_units(saved, default_unit)
        else:
            # No cartons or boxes: the line is in the product's own unit and shows no packing.
            for name in PURCHASE_LINE_UNIT_FIELDS:
                del self.fields[name]

    def _init_units(self, saved, default_unit):
        """The unit dropdown and the two packing boxes of a row."""
        unit_field = self.fields["unit_level"]
        # A POST without the unit leaves the line as it is: a new line is in pieces, as before.
        unit_field.required = False
        unit_field.widget.attrs.update(
            {
                "aria-label": "Unit",
                "data-tip": "Bought by the carton, box or piece. In the quantity box, press C, B or P.",
            }
        )
        tips = {
            "boxes_per_carton": "Boxes in one carton. 1 means no cartons. Saved to the product with the purchase.",
            "pieces_per_box": "Pieces in one box. 1 means no boxes. Saved to the product with the purchase.",
        }
        for name in PACKING_FIELDS:
            field = self.fields[name]
            field.widget.attrs.update(
                {
                    "class": "form-control form-control-sm",
                    "step": "1",
                    "inputmode": "numeric",
                    "aria-label": field.label,
                    "data-tip": tips[name],
                }
            )
        if saved is not None:
            for name in PACKING_FIELDS:
                self.initial[name] = getattr(saved.product, name)
        else:
            # A blank row starts in the shop's stock unit; the browser narrows it to the chosen product.
            self.initial["unit_level"] = normalize_pack_level(default_unit, default=None) or default_stock_unit()
        unit_field.widget.choices = self._unit_choices(saved)

    def _row_product(self, saved):
        """The product this row shows: the posted one on a bound form, otherwise the saved line's."""
        if not self.is_bound:
            return saved.product if saved is not None else None
        raw = self.data.get(self.add_prefix("product"))
        if not raw:
            return None
        if saved is not None and str(saved.product_id) == str(raw):
            return saved.product
        try:
            return self.fields["product"].queryset.filter(pk=raw).first()
        except (TypeError, ValueError):
            return None

    def _posted_packing(self, name, fallback):
        try:
            value = int(self.data.get(self.add_prefix(name)) or 0)
        except (TypeError, ValueError):
            return fallback
        return value if value >= 1 else fallback

    def _unit_choices(self, saved):
        """What the unit dropdown offers on this row: the levels its product has, or all three."""
        product = self._row_product(saved)
        if product is None:
            return [(code, PACK_NAMES[code]) for code in PACK_LEVELS]
        per_box, per_carton = product.pieces_per_box, product.boxes_per_carton
        if self.is_bound:
            # Sent back with what was typed: the dropdown follows the packing on the row.
            per_box = self._posted_packing("pieces_per_box", per_box)
            per_carton = self._posted_packing("boxes_per_carton", per_carton)
        shape = Product(unit=product.unit, pieces_per_box=per_box, boxes_per_carton=per_carton)
        names = {
            level["code"]: level["label"] if level["code"] == PACK_PIECE else level["name"]
            for level in shape.pack_levels
        }
        if saved is not None and saved.product_id == product.pk:
            # The packing may have changed since the line was written: its own unit still stands.
            names.setdefault(self.saved_line["level"], PACK_NAMES[self.saved_line["level"]])
        return [(code, names[code]) for code in PACK_LEVELS if code in names]

    def has_changed(self):
        if self.instance.pk:
            return super().has_changed()
        # A new row is a line once a product, a quantity or a price is on it; its unit and
        # packing boxes hold defaults and do not count.
        return any(name not in PURCHASE_LINE_UNIT_FIELDS for name in self.changed_data)

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        if product is None or "pieces_per_box" not in self.fields:
            return cleaned
        if any(name in self.errors for name in PACKING_FIELDS):
            return cleaned
        # An empty box, or a POST without the boxes, leaves the product's packing as it is.
        per_box = cleaned.get("pieces_per_box") or product.pieces_per_box
        per_carton = cleaned.get("boxes_per_carton") or product.boxes_per_carton
        if (per_box, per_carton) == (product.pieces_per_box, product.boxes_per_carton):
            return cleaned
        if per_box > 1 and product.unit == "box":
            self.add_error(
                "pieces_per_box",
                f"{product.display_name} is counted by the box. Set its unit to Piece on the product "
                "before a box of it can hold pieces.",
            )
            return cleaned
        # PurchaseItem.clean() sizes the line's unit from this product, so it holds the new packing first.
        product.pieces_per_box, product.boxes_per_carton = per_box, per_carton
        self.packing_change = (per_box, per_carton)
        # A line saved earlier is resized by the corrected packing too.
        self.instance.refresh_unit_factor = True
        return cleaned


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
        fields = ["name", "tax_status", "gst_rate", "cash_gst_rate", "bank_gst_rate", "is_active"]
        labels = {
            "tax_status": "Tax status",
            "gst_rate": "Default tax rate (%)",
            "cash_gst_rate": "Cash tax rate (%)",
            "bank_gst_rate": "Bank tax rate (%)",
        }
        help_texts = {
            "tax_status": "Taxable categories are taxed automatically in POS when category tax mode is on.",
            "gst_rate": "The category's base rate, also used when the cash and bank rates are left blank.",
            "cash_gst_rate": "Applied automatically when this category is sold as a cash sale in category tax mode.",
            "bank_gst_rate": "Applied automatically when this category is sold as a bank sale in category tax mode.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["cash_gst_rate"].required = False
        self.fields["bank_gst_rate"].required = False

    def clean(self):
        cleaned_data = super().clean()
        base_rate = cleaned_data.get("gst_rate")
        if cleaned_data.get("tax_status") != Category.TAXABLE:
            cleaned_data["gst_rate"] = Decimal("0")
            cleaned_data["cash_gst_rate"] = Decimal("0")
            cleaned_data["bank_gst_rate"] = Decimal("0")
            self.instance.gst_rate = Decimal("0")
            self.instance.cash_gst_rate = Decimal("0")
            self.instance.bank_gst_rate = Decimal("0")
            return cleaned_data

        if cleaned_data.get("cash_gst_rate") in (None, ""):
            cleaned_data["cash_gst_rate"] = base_rate if base_rate not in (None, "") else Decimal("0")
            self.instance.cash_gst_rate = cleaned_data["cash_gst_rate"]
        if cleaned_data.get("bank_gst_rate") in (None, ""):
            cleaned_data["bank_gst_rate"] = base_rate if base_rate not in (None, "") else Decimal("0")
            self.instance.bank_gst_rate = cleaned_data["bank_gst_rate"]
        return cleaned_data


class QuickCategoryForm(StyledModelForm):
    class Meta:
        model = Category
        fields = ["name"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


class BasePurchaseItemFormSet(BaseModelFormSet):
    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        if "default_unit" not in kwargs:
            # The shop's stock unit is read once for the table, not once for every line.
            if not hasattr(self, "_default_unit"):
                self._default_unit = default_stock_unit() if packaging_enabled() else PACK_PIECE
            kwargs["default_unit"] = self._default_unit
        return kwargs

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
    quantity_delta = forms.DecimalField(required=False, max_digits=12, decimal_places=3, label="Adjustment quantity")
    quantity_after = forms.DecimalField(required=False, min_value=0, max_digits=12, decimal_places=3, label="Counted stock")
    # A packed product is adjusted and counted in the unit chosen here; the quantity is kept in base units.
    quantity_delta_unit = forms.ChoiceField(required=False, choices=PACK_LEVEL_CHOICES, label="Adjustment unit")
    quantity_after_unit = forms.ChoiceField(required=False, choices=PACK_LEVEL_CHOICES, label="Counted stock unit")
    reason_preset = forms.ChoiceField(choices=REASON_CHOICES, label="Reason")
    custom_reason = forms.CharField(required=False, max_length=150, label="Custom reason")
    reason = forms.CharField(max_length=150, widget=forms.HiddenInput())
    notes = forms.CharField(required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_stock_unit = PACK_PIECE
        if packaging_enabled():
            self.default_stock_unit = default_stock_unit()
            for name in ("quantity_delta_unit", "quantity_after_unit"):
                self.fields[name].choices = pack_unit_choices()
                self.fields[name].initial = self.default_stock_unit
                self.fields[name].widget.attrs["aria-label"] = self.fields[name].label
        else:
            del self.fields["quantity_delta_unit"]
            del self.fields["quantity_after_unit"]
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

        product = cleaned_data.get("product")
        for name in ("quantity_delta", "quantity_after"):
            quantity = cleaned_data.get(name)
            if product is None or quantity is None:
                continue
            # A plain product has one unit, whatever the picker was left on.
            level = normalize_pack_level(cleaned_data.get(f"{name}_unit")) if product.has_packaging else PACK_PIECE
            base = typed_quantity_to_base(self, product, name, quantity, level)
            if base is not None:
                cleaned_data[name] = base

        if reason_preset == "other":
            if not custom_reason:
                self.add_error("custom_reason", "Enter the reason when Other is selected.")
            cleaned_data["reason"] = custom_reason
        else:
            reason_map = dict(self.REASON_CHOICES)
            cleaned_data["reason"] = reason_map.get(reason_preset, "")

        return cleaned_data




class SaleReturnForm(BankReceiptFormMixin, StyledFieldsMixin, forms.Form):
    # A bank refund may carry its receipt; it is never demanded.
    bank_receipt_method_fields = ("refund_method",)
    bank_receipt_required = False

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


class PurchaseReturnForm(BankReceiptFormMixin, StyledFieldsMixin, forms.Form):
    bank_receipt_method_fields = ("settlement_method",)
    bank_receipt_required = False

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







