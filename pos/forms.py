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
from accounts.services.cash_bank import bank_account_queryset
from .services.access import (
    ACCESS_CHOICES,
    CASHIER_ROLE,
    MODULE_DEFINITIONS,
    get_role_choices,
)
from .services.modules import allowed_unit_choices, locked_settings_flags, module_enabled, module_within_ceiling
from .services.user_timezones import normalize_time_zone_name, time_zone_choices


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
            "allow_price_override",
            "allow_line_discount",
            "allow_zero_total_sales",
            "default_sale_payment_method",
            "thermal_printer_width_mm",
            "currency_symbol",
            "currency_code",
            "default_markup_percent",
            "invoice_prefix",
        ]
        labels = {
            "gst_enabled": "Charge tax on sales",
            "tax_mode": "Tax mode",
            "tax_label": "Tax label",
            "use_payment_method_gst": "Different tax rate for cash and bank",
            "cash_gst_rate": "Cash tax rate (%)",
            "bank_gst_rate": "Bank tax rate (%)",
            "enable_product_expiry": "Enable product expiry tracking",
            "expiry_alert_days": "Expiry alert window (days)",
            "allow_credit_sales": "Allow credit sales",
            "allow_price_override": "Allow price override",
            "allow_line_discount": "Allow line discount",
            "allow_zero_total_sales": "Allow zero-total sales",
            "default_sale_payment_method": "Default payment method",
            "thermal_printer_width_mm": "Thermal printer width (mm)",
            "currency_symbol": "Currency symbol",
            "currency_code": "Currency code",
            "default_markup_percent": "Default markup on cost (%)",
            "invoice_prefix": "Invoice number prefix",
        }
        help_texts = {
            "tax_mode": "Category tax applies each product line's rate from its category. Manual cart tax lets the cashier type one rate for the whole sale.",
            "tax_label": "Examples: GST, VAT, Sales Tax.",
            "use_payment_method_gst": "When enabled, Cash and Bank sales use the two rates below and the POS tax box becomes automatic.",
            "cash_gst_rate": "Applied when the sale is completed as a cash sale.",
            "bank_gst_rate": "Applied when the sale is completed as a bank sale.",
            "enable_product_expiry": "Show expiry date fields across products and purchases.",
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
        self.fields["expiry_alert_days"].required = False
        # Optional so a settings POST that predates these fields keeps the stored values.
        self.fields["currency_symbol"].required = False
        self.fields["currency_code"].required = False
        self.fields["default_markup_percent"].required = False
        self.fields["default_markup_percent"].widget.attrs.update({"min": "0", "step": "0.01"})
        self.fields["currency_code"].widget.attrs.update({"maxlength": "3", "style": "text-transform: uppercase;"})
        # Switches of modules the shop cannot use are read-only: a disabled field ignores the POST,
        # so nobody can turn one on by hand-crafting a request.
        self.locked_flags = locked_settings_flags()
        for flag in self.locked_flags:
            if flag in self.fields:
                self.fields[flag].disabled = True
        if not module_enabled("markup"):
            self.fields["default_markup_percent"].disabled = True

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
        return cleaned_data


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
    if not module_enabled("markup"):
        # Only "type the price" is left: no chooser, no markup inputs.
        for name in ("price_by", "markup_percent", "markup_amount"):
            form.fields.pop(name, None)
    # Expiry dates stay editable while tracking is merely switched off (only the alerts go quiet);
    # the field disappears when the edition does not include expiry tracking at all.
    if not module_within_ceiling("expiry"):
        form.fields.pop("expiry_date", None)


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
            "cost_price",
            "markup_percent",
            "markup_amount",
            "price",
            "minimum_margin_percent",
            "low_stock_threshold",
            "location",
            "expiry_date",
            "is_active",
            *PRODUCT_STOCK_SWITCH_FIELDS,
        ]
        labels = {
            "sku": "SKU",
            "unit": "Sold by",
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
        }

    def __init__(self, *args, variant_source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["price"].required = False
        self.fields["unit"].required = False
        self.initial["track_stock"] = "1" if self.instance.track_stock else "0"
        self.initial["price_by"] = self.instance.price_by
        if self.instance.pk:
            del self.fields["opening_stock"]
            self.initial["low_stock_threshold"] = format_qty(self.instance.low_stock_threshold)
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

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("unit"):
            cleaned["unit"] = self.instance.unit or Product.UNIT_PIECE
            self.instance.unit = cleaned["unit"]
        if "markup_percent" not in self.fields:
            # Markup Pricing is off: the typed price is the price, so a stored markup must not overrule it.
            self.instance.markup_percent = None
            self.instance.markup_amount = None
        self._clean_price_by(cleaned)
        markup = cleaned.get("markup_percent")
        markup_amount = cleaned.get("markup_amount")
        cost = cleaned.get("cost_price")
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

        opening_stock = cleaned.get("opening_stock")
        unit = cleaned.get("unit")
        if opening_stock and unit not in Product.FRACTIONAL_UNITS and opening_stock != opening_stock.to_integral_value():
            self.add_error("opening_stock", "This unit is counted in whole numbers.")
        return cleaned

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
        return cleaned

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


class PurchaseForm(StyledModelForm):
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


class PurchaseProductSelect(forms.Select):
    """Product dropdown whose options carry unit and pricing rules for the purchase line script."""

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        product = getattr(value, "instance", None)
        if product is not None:
            option["attrs"].update(
                {
                    "data-unit": product.unit,
                    "data-step": product.quantity_step,
                    "data-markup": "" if product.markup_percent is None else str(product.markup_percent),
                    "data-markup-amount": "" if product.markup_amount is None else str(product.markup_amount),
                    "data-min-margin": str(product.minimum_margin_percent or 0),
                    "data-price": str(product.price or ""),
                }
            )
        return option


class PurchaseItemForm(StyledModelForm):
    class Meta:
        model = PurchaseItem
        fields = ["product", "quantity", "cost_price", "sale_price"]
        labels = {
            "cost_price": "Unit cost",
            "sale_price": "Sale price",
        }
        help_texts = {
            "sale_price": "The purchase will update the product's active sale price.",
        }
        widgets = {
            "product": PurchaseProductSelect,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.empty_permitted = True
        # Each size is its own choice, named "Bread - Small".
        self.fields["product"].queryset = Product.objects.filter(is_active=True).order_by("name", "variant_label")
        self.fields["quantity"].widget.attrs.update({"min": "0", "inputmode": "decimal"})
        self.fields["sale_price"].required = False
        if self.instance.pk and self.instance.product_id:
            # New rows get their step from the chosen option in the browser.
            self.fields["quantity"].widget.attrs["step"] = self.instance.product.quantity_step
            self.initial["quantity"] = format_qty(self.instance.quantity)


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
    reason_preset = forms.ChoiceField(choices=REASON_CHOICES, label="Reason")
    custom_reason = forms.CharField(required=False, max_length=150, label="Custom reason")
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







