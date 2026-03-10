from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


TWO_PLACES = Decimal("0.01")


def quantize_money(value):
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


class Category(models.Model):
    TAXABLE = "taxable"
    ZERO_RATED = "zero_rated"
    EXEMPT = "exempt"
    TAX_STATUS_CHOICES = (
        (TAXABLE, "Taxable"),
        (ZERO_RATED, "Zero Rated"),
        (EXEMPT, "Exempt"),
    )

    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    tax_status = models.CharField(max_length=20, choices=TAX_STATUS_CHOICES, default=EXEMPT)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cash_gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    bank_gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    def __str__(self):
        return self.name

    @property
    def effective_gst_rate(self):
        return self.gst_rate_for_payment_method("cash")

    def gst_rate_for_payment_method(self, payment_method):
        if self.tax_status != self.TAXABLE:
            return Decimal("0")
        if payment_method == "bank":
            return quantize_money(self.bank_gst_rate if Decimal(self.bank_gst_rate or 0) > 0 else (self.gst_rate or 0))
        if payment_method == "cash":
            return quantize_money(self.cash_gst_rate if Decimal(self.cash_gst_rate or 0) > 0 else (self.gst_rate or 0))
        return quantize_money(self.gst_rate or 0)

    def clean(self):
        errors = {}
        rates = {
            "gst_rate": Decimal(self.gst_rate or 0),
            "cash_gst_rate": Decimal(self.cash_gst_rate or 0),
            "bank_gst_rate": Decimal(self.bank_gst_rate or 0),
        }

        for field_name, rate in rates.items():
            if rate < 0:
                errors[field_name] = "GST rate cannot be negative."
            if rate > 100:
                errors[field_name] = "GST rate cannot exceed 100%."

        if self.tax_status != self.TAXABLE:
            self.gst_rate = Decimal("0")
            self.cash_gst_rate = Decimal("0")
            self.bank_gst_rate = Decimal("0")

        if errors:
            raise ValidationError(errors)

class Brand(models.Model):
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Supplier(models.Model):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.name

    @property
    def raw_balance(self):
        from accounts.services.party_balance import get_party_balance

        return get_party_balance(self)


class Product(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="products",
    )
    name = models.CharField(max_length=100)
    sku = models.CharField(max_length=50, unique=True)
    barcode = models.CharField(max_length=50, unique=True, blank=True, null=True)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Selling price")
    minimum_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    stock = models.PositiveIntegerField(default=0)
    low_stock_threshold = models.PositiveIntegerField(
        default=5,
        help_text="Alert when stock falls to this level",
    )
    location = models.CharField(max_length=100, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def minimum_sale_price_for_cost(self, cost_price=None):
        cost = Decimal(cost_price if cost_price is not None else self.cost_price or 0)
        margin = Decimal(self.minimum_margin_percent or 0)
        multiplier = Decimal("1") + (margin / Decimal("100"))
        return quantize_money(cost * multiplier)

    @property
    def minimum_sale_price(self):
        return self.minimum_sale_price_for_cost()

    @property
    def current_margin_percent(self):
        cost = Decimal(self.cost_price or 0)
        if cost <= 0:
            return None
        price = Decimal(self.price or 0)
        margin = ((price - cost) / cost) * Decimal("100")
        return quantize_money(margin)

    @property
    def margin_status(self):
        margin = self.current_margin_percent
        if margin is None:
            return "unknown"
        if margin < Decimal(self.minimum_margin_percent or 0):
            return "below"
        return "healthy"

    def clean(self):
        errors = {}

        if Decimal(self.cost_price or 0) < 0:
            errors["cost_price"] = "Cost price cannot be negative."
        if Decimal(self.price or 0) < 0:
            errors["price"] = "Sale price cannot be negative."
        if Decimal(self.minimum_margin_percent or 0) < 0:
            errors["minimum_margin_percent"] = "Minimum margin cannot be negative."

        if Decimal(self.price or 0) and Decimal(self.cost_price or 0):
            minimum_sale_price = self.minimum_sale_price
            if Decimal(self.price) < minimum_sale_price:
                errors["price"] = (
                    f"Sale price must be at least {minimum_sale_price} to keep the minimum margin."
                )

        if errors:
            raise ValidationError(errors)


class Customer(models.Model):
    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.name

    @property
    def balance(self):
        from accounts.services.party_balance import get_party_balance

        return get_party_balance(self)


class Sale(models.Model):
    CASH = "cash"
    BANK = "bank"
    TAX_MODE_MANUAL = "manual_cart"
    TAX_MODE_CATEGORY = "category_gst"
    PAYMENT_METHOD_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )
    TAX_MODE_CHOICES = (
        (TAX_MODE_MANUAL, "Manual Cart Tax"),
        (TAX_MODE_CATEGORY, "Category GST"),
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales",
    )
    sold_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales_made",
    )
    is_credit = models.BooleanField(default=False, help_text="Sale on credit")
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    bank_account = models.ForeignKey("accounts.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="sales")
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    discount_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_mode = models.CharField(max_length=20, choices=TAX_MODE_CHOICES, default=TAX_MODE_MANUAL)
    tax_label = models.CharField(max_length=30, default="GST")
    cash_tax_rate_snapshot = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    bank_tax_rate_snapshot = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"Sale #{self.id}"

    def clean(self):
        errors = {}
        paid_amount = Decimal(self.paid_amount or 0)

        if paid_amount < 0:
            errors["paid_amount"] = "Paid amount cannot be negative."
        if self.is_credit or self.payment_method == self.CASH:
            self.bank_account = None

        if errors:
            raise ValidationError(errors)

    def safe_decimal(self, value):
        try:
            return Decimal(value)
        except (TypeError, InvalidOperation):
            return Decimal("0")

    @property
    def subtotal(self):
        return sum((item.line_total for item in self.items.all()), Decimal("0"))

    @property
    def balance_due(self):
        return quantize_money(max(Decimal(self.total_amount or 0) - Decimal(self.paid_amount or 0), Decimal("0")))

    @property
    def net_sales_amount(self):
        return quantize_money(Decimal(self.total_amount or 0) - Decimal(self.tax_amount or 0))

    def _selected_tax_rate(self):
        if self.tax_mode == self.TAX_MODE_CATEGORY:
            return Decimal("0")
        if self.payment_method == self.BANK and Decimal(self.bank_tax_rate_snapshot or 0) > 0:
            return Decimal(self.bank_tax_rate_snapshot or 0)
        if self.payment_method == self.CASH and Decimal(self.cash_tax_rate_snapshot or 0) > 0:
            return Decimal(self.cash_tax_rate_snapshot or 0)
        return min(max(self.safe_decimal(self.tax_rate), Decimal("0")), Decimal("100"))

    def _category_tax_amount(self, items, discount_amount, payment_method=None):
        subtotal = sum((item.line_total for item in items), Decimal("0"))
        if subtotal <= 0:
            return Decimal("0")

        remaining_discount = quantize_money(discount_amount)
        total_tax = Decimal("0")

        for index, item in enumerate(items):
            if index == len(items) - 1:
                allocated_discount = remaining_discount
            else:
                share = Decimal(item.line_total) / subtotal
                allocated_discount = quantize_money(Decimal(discount_amount) * share)
                remaining_discount -= allocated_discount

            taxable_base = max(Decimal(item.line_total) - Decimal(allocated_discount), Decimal("0"))
            tax_rate = Decimal(item.tax_rate_for_payment_method(payment_method or self.payment_method) if hasattr(item, "tax_rate_for_payment_method") else 0)
            total_tax += quantize_money(taxable_base * (tax_rate / Decimal("100")))

        return quantize_money(total_tax)

    def update_total(self):
        items = list(self.items.select_related("product__category"))
        subtotal = sum((item.line_total for item in items), Decimal("0"))

        discount_rate = min(max(self.safe_decimal(self.discount_rate), Decimal("0")), Decimal("100"))
        discount_amount = subtotal * (discount_rate / Decimal("100"))
        after_discount = subtotal - discount_amount
        if self.tax_mode == self.TAX_MODE_CATEGORY:
            tax_amount = self._category_tax_amount(items, discount_amount, payment_method=self.payment_method)
            self.tax_rate = Decimal("0")
        else:
            selected_rate = self._selected_tax_rate()
            self.tax_rate = quantize_money(selected_rate)
            tax_amount = after_discount * (selected_rate / Decimal("100"))

        self.tax_amount = quantize_money(tax_amount)
        self.total_amount = quantize_money(after_discount + tax_amount)
        self.save(update_fields=["tax_rate", "tax_amount", "total_amount"])


class SaleItem(models.Model):
    sale = models.ForeignKey(Sale, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    original_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cash_tax_rate_snapshot = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    bank_tax_rate_snapshot = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    def tax_rate_for_payment_method(self, payment_method):
        if payment_method == Sale.BANK:
            rate = Decimal(self.bank_tax_rate_snapshot or 0)
        else:
            rate = Decimal(self.cash_tax_rate_snapshot or 0)
        if rate > 0:
            return quantize_money(rate)
        category = getattr(self.product, "category", None)
        if category:
            return quantize_money(category.gst_rate_for_payment_method(payment_method))
        return Decimal("0")

    @property
    def discounted_unit_price(self):
        discount = Decimal(self.discount_percent or 0)
        multiplier = Decimal("1") - (discount / Decimal("100"))
        return quantize_money(Decimal(self.price) * multiplier)

    @property
    def base_line_total(self):
        return quantize_money(Decimal(self.price) * Decimal(self.quantity))

    @property
    def line_total(self):
        return quantize_money(self.discounted_unit_price * Decimal(self.quantity))

    @property
    def line_discount_amount(self):
        return quantize_money(self.base_line_total - self.line_total)

    @property
    def line_cost(self):
        return quantize_money(Decimal(self.cost_price) * Decimal(self.quantity))

    def clean(self):
        errors = {}
        if self.quantity <= 0:
            errors["quantity"] = "Quantity must be greater than zero."
        if Decimal(self.price or 0) <= 0:
            errors["price"] = "Price must be greater than zero."
        if Decimal(self.original_price or 0) < 0:
            errors["original_price"] = "Original price cannot be negative."
        if Decimal(self.cost_price or 0) < 0:
            errors["cost_price"] = "Cost price cannot be negative."

        discount_percent = Decimal(self.discount_percent or 0)
        if discount_percent < 0 or discount_percent > 100:
            errors["discount_percent"] = "Discount percent must be between 0 and 100."

        minimum_price = self.product.minimum_sale_price_for_cost(self.cost_price or self.product.cost_price)
        if self.discounted_unit_price < minimum_price:
            errors["price"] = f"Final unit price cannot go below {minimum_price}."

        available_stock = self.product.stock
        if self.pk:
            old_item = SaleItem.objects.select_related("product").get(pk=self.pk)
            if old_item.product_id == self.product_id:
                available_stock += old_item.quantity
        if self.quantity > available_stock:
            errors["quantity"] = f"Not enough stock for {self.product.name}."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        if is_new and not self.original_price:
            self.original_price = self.product.price
        if is_new and not self.price:
            self.price = self.product.price
        if is_new and self.discount_percent in (None, ""):
            self.discount_percent = Decimal("0")
        if is_new and not self.cost_price:
            self.cost_price = self.product.cost_price

        self.clean()

        if is_new:
            self.product.stock -= self.quantity
            self.product.save(update_fields=["stock"])
        else:
            old_item = SaleItem.objects.select_related("product").get(pk=self.pk)
            if old_item.product_id == self.product_id:
                self.product.stock = self.product.stock + old_item.quantity - self.quantity
                self.product.save(update_fields=["stock"])
            else:
                old_product = old_item.product
                old_product.stock += old_item.quantity
                old_product.save(update_fields=["stock"])

                self.product.stock -= self.quantity
                self.product.save(update_fields=["stock"])

        super().save(*args, **kwargs)
        self.sale.update_total()

    def delete(self, *args, **kwargs):
        self.product.stock += self.quantity
        self.product.save(update_fields=["stock"])

        sale = self.sale
        super().delete(*args, **kwargs)
        sale.update_total()

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

class Purchase(models.Model):
    CASH = "cash"
    BANK = "bank"
    PAYMENT_METHOD_CHOICES = (
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="purchases",
    )
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    bank_account = models.ForeignKey("accounts.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="purchases")
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="edited_purchases",
    )
    last_edited_at = models.DateTimeField(null=True, blank=True)
    edit_reason = models.TextField(blank=True)
    def clean(self):
        errors = {}
        if Decimal(self.paid_amount or 0) < 0:
            errors["paid_amount"] = "Paid amount cannot be negative."
        if self.payment_method == self.CASH or Decimal(self.paid_amount or 0) == 0:
            self.bank_account = None
        if errors:
            raise ValidationError(errors)

        if errors:
            raise ValidationError(errors)

    @property
    def balance_due(self):
        return quantize_money(max(Decimal(self.total_amount or 0) - Decimal(self.paid_amount or 0), Decimal("0")))

    @property
    def payment_status(self):
        if self.total_amount <= 0:
            return "draft"
        if self.balance_due <= 0:
            return "paid"
        if Decimal(self.paid_amount or 0) > 0:
            return "partial"
        return "credit"

    def update_total(self):
        total = Decimal("0")
        for item in self.items.all():
            total += item.line_total

        self.total_amount = quantize_money(total)
        self.save(update_fields=["total_amount"])


class PurchaseItem(models.Model):
    purchase = models.ForeignKey(Purchase, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    cost_price = models.DecimalField(max_digits=10, decimal_places=2)
    sale_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    @property
    def line_total(self):
        return quantize_money(Decimal(self.cost_price) * Decimal(self.quantity))

    @property
    def margin_percent(self):
        cost = Decimal(self.cost_price or 0)
        if cost <= 0:
            return None
        return quantize_money(((Decimal(self.sale_price or 0) - cost) / cost) * Decimal("100"))

    @property
    def total_price(self):
        return self.line_total

    def clean(self):
        if not self.product_id and not self.quantity and not self.cost_price and not self.sale_price:
            return

        errors = {}
        if not self.product_id:
            errors["product"] = "Product is required."
        if self.quantity in (None, "") or self.quantity <= 0:
            errors["quantity"] = "Quantity must be greater than zero."
        if self.cost_price in (None, "") or Decimal(self.cost_price or 0) <= 0:
            errors["cost_price"] = "Cost price must be greater than zero."
        if self.sale_price in (None, "") or Decimal(self.sale_price or 0) <= 0:
            errors["sale_price"] = "Sale price must be greater than zero."

        if self.product_id and self.cost_price not in (None, "") and self.sale_price not in (None, ""):
            minimum_sale_price = self.product.minimum_sale_price_for_cost(self.cost_price)
            if Decimal(self.sale_price or 0) < minimum_sale_price:
                errors["sale_price"] = (
                    f"Sale price must be at least {minimum_sale_price} for the configured minimum margin."
                )

        if errors:
            raise ValidationError(errors)

    def _sync_product_pricing(self):
        updated_fields = []
        if self.purchase.supplier_id and self.product.supplier_id != self.purchase.supplier_id:
            self.product.supplier = self.purchase.supplier
            updated_fields.append("supplier")
        if Decimal(self.product.cost_price or 0) != Decimal(self.cost_price or 0):
            self.product.cost_price = self.cost_price
            updated_fields.append("cost_price")
        if Decimal(self.product.price or 0) != Decimal(self.sale_price or 0):
            self.product.price = self.sale_price
            updated_fields.append("price")

        if updated_fields:
            self.product.save(update_fields=updated_fields)

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None

        if is_new:
            self.product.stock += self.quantity
            self.product.save(update_fields=["stock"])
            old_total = Decimal("0")
        else:
            old_item = PurchaseItem.objects.select_related("product").get(pk=self.pk)
            old_total = self.purchase.total_amount

            if old_item.product_id == self.product_id:
                new_stock = self.product.stock + (self.quantity - old_item.quantity)
                if new_stock < 0:
                    raise ValidationError("Cannot reduce stock below zero")
                self.product.stock = new_stock
                self.product.save(update_fields=["stock"])
            else:
                old_product = old_item.product
                restored_old_stock = old_product.stock - old_item.quantity
                if restored_old_stock < 0:
                    raise ValidationError(
                        "Cannot change this purchase item because existing stock has already been consumed"
                    )
                old_product.stock = restored_old_stock
                old_product.save(update_fields=["stock"])

                self.product.stock += self.quantity
                self.product.save(update_fields=["stock"])

        super().save(*args, **kwargs)
        self._sync_product_pricing()
        self.purchase.update_total()

    def delete(self, *args, **kwargs):
        if self.product.stock < self.quantity:
            raise ValidationError("Cannot reduce stock below zero")

        self.product.stock -= self.quantity
        self.product.save(update_fields=["stock"])

        purchase = self.purchase
        super().delete(*args, **kwargs)
        purchase.update_total()

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class PurchaseEditLog(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="edit_logs")
    edited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    edited_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()
    old_total = models.DecimalField(max_digits=10, decimal_places=2)
    new_total = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"Purchase #{self.purchase.id} edited by {self.edited_by}"


class LedgerEntry(models.Model):
    DEBIT = "debit"
    CREDIT = "credit"

    ENTRY_TYPE_CHOICES = (
        (DEBIT, "Debit"),
        (CREDIT, "Credit"),
    )

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    party = GenericForeignKey("content_type", "object_id")
    entry_type = models.CharField(max_length=6, choices=ENTRY_TYPE_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=50, blank=True, help_text="Sale / Purchase / Payment / Loan")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.party} | {self.entry_type} | {self.amount}"


class UserSessionLog(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="session_logs")
    role_name = models.CharField(max_length=80)
    session_key = models.CharField(max_length=40, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    login_at = models.DateTimeField(default=timezone.now)
    logout_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-login_at"]

    def __str__(self):
        return f"{self.user} | {self.role_name} | {self.login_at:%Y-%m-%d %H:%M}"



class CustomRole(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    description = models.CharField(max_length=255, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def group_name(self):
        return f"CustomRole:{self.slug}"

    def ensure_group(self):
        from django.contrib.auth.models import Group

        Group.objects.get_or_create(name=self.group_name)

    def clean(self):
        self.name = (self.name or "").strip()
        self.description = (self.description or "").strip()
        if not self.name:
            raise ValidationError({"name": "Role name is required."})

        reserved_names = {"admin", "cashier"}
        if self.name.lower() in reserved_names:
            raise ValidationError({"name": "This role name is reserved for the built-in system roles."})

        duplicate_qs = type(self).objects.exclude(pk=self.pk).filter(name__iexact=self.name)
        if duplicate_qs.exists():
            raise ValidationError({"name": "A role with this name already exists."})

    def _build_unique_slug(self):
        base_slug = slugify(self.name) or "role"
        candidate = base_slug
        counter = 2
        while type(self).objects.exclude(pk=self.pk).filter(slug=candidate).exists():
            candidate = f"{base_slug}-{counter}"
            counter += 1
        return candidate

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.description = (self.description or "").strip()
        if not self.slug:
            self.slug = self._build_unique_slug()
        super().save(*args, **kwargs)
        self.ensure_group()


class CompanyProfile(models.Model):
    name = models.CharField(max_length=200)
    logo = models.ImageField(upload_to="company_logos/", blank=True, null=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    tax_registration_number = models.CharField(
        max_length=100,
        blank=True,
        help_text="GST, VAT, NTN, or other tax registration number shown on receipts.",
    )
    invoice_footer = models.TextField(blank=True, help_text="Text shown at bottom of invoice")
    show_qr = models.BooleanField(default=True)
    qr_note = models.CharField(max_length=255, blank=True, help_text="Text below QR code")
    is_active = models.BooleanField(default=True, help_text="Only one company profile should be active")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Company Profile"
        verbose_name_plural = "Company Profile"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_active:
            CompanyProfile.objects.exclude(pk=self.pk).filter(is_active=True).update(is_active=False)


class SystemSettings(models.Model):
    MANUAL_CART = "manual_cart"
    CATEGORY_GST = "category_gst"
    TAX_MODE_CHOICES = (
        (MANUAL_CART, "Manual Cart Tax"),
        (CATEGORY_GST, "Category GST"),
    )

    gst_enabled = models.BooleanField(default=False)
    tax_mode = models.CharField(max_length=20, choices=TAX_MODE_CHOICES, default=MANUAL_CART)
    tax_label = models.CharField(max_length=30, default="GST")
    use_payment_method_gst = models.BooleanField(default=False)
    cash_gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    bank_gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    enable_product_expiry = models.BooleanField(default=False)
    expiry_alert_days = models.PositiveIntegerField(default=30)
    allow_credit_sales = models.BooleanField(default=True)
    allow_price_override = models.BooleanField(default=True)
    allow_line_discount = models.BooleanField(default=True)
    default_sale_payment_method = models.CharField(
        max_length=10,
        choices=Sale.PAYMENT_METHOD_CHOICES,
        default=Sale.CASH,
    )
    thermal_printer_width_mm = models.PositiveIntegerField(default=50)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "System Settings"
        verbose_name_plural = "System Settings"

    def __str__(self):
        return "System Settings"

    @classmethod
    def load(cls):
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings

    @property
    def uses_category_gst(self):
        return self.gst_enabled and self.tax_mode == self.CATEGORY_GST

    @property
    def uses_payment_method_gst(self):
        return self.gst_enabled and self.tax_mode == self.MANUAL_CART and self.use_payment_method_gst

    @property
    def allows_manual_tax(self):
        return self.gst_enabled and self.tax_mode == self.MANUAL_CART and not self.use_payment_method_gst

    def gst_rate_for_payment_method(self, payment_method):
        if payment_method == Sale.BANK:
            return quantize_money(Decimal(self.bank_gst_rate or 0))
        return quantize_money(Decimal(self.cash_gst_rate or 0))

    def save(self, *args, **kwargs):
        self.pk = 1
        self.tax_label = (self.tax_label or "GST").strip() or "GST"
        self.thermal_printer_width_mm = max(int(self.thermal_printer_width_mm or 50), 50)
        self.expiry_alert_days = max(int(self.expiry_alert_days or 30), 1)
        self.cash_gst_rate = quantize_money(max(Decimal(self.cash_gst_rate or 0), Decimal("0")))
        self.bank_gst_rate = quantize_money(max(Decimal(self.bank_gst_rate or 0), Decimal("0")))
        super().save(*args, **kwargs)

class StockAdjustment(models.Model):
    ADJUSTMENT = "adjustment"
    STOCK_TAKE = "stock_take"
    MODE_CHOICES = (
        (ADJUSTMENT, "Stock Adjustment"),
        (STOCK_TAKE, "Stock Take"),
    )

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_adjustments")
    adjustment_mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=ADJUSTMENT)
    quantity_before = models.PositiveIntegerField(default=0)
    quantity_after = models.PositiveIntegerField(default=0)
    quantity_delta = models.IntegerField(default=0)
    reason = models.CharField(max_length=150)
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "accounts.Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_adjustments",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_adjustments_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Stock adjustment #{self.id} - {self.product.name}"


class SaleReturn(models.Model):
    CASH = "cash"
    BANK = "bank"
    RECEIVABLE = "receivable"
    REFUND_METHOD_CHOICES = (
        (CASH, "Cash Refund"),
        (BANK, "Bank Refund"),
        (RECEIVABLE, "Reduce Receivable"),
    )

    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="returns")
    customer = models.ForeignKey(Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="sale_returns")
    refund_method = models.CharField(max_length=20, choices=REFUND_METHOD_CHOICES, default=CASH)
    bank_account = models.ForeignKey(
        "accounts.BankAccount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sale_returns",
    )
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=150)
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "accounts.Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sale_returns",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sale_returns_processed",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Sale return #{self.id} for sale #{self.sale_id}"


class SaleReturnItem(models.Model):
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT, related_name="return_items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_return_items")
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_amount = models.DecimalField(max_digits=12, decimal_places=2)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class PurchaseReturn(models.Model):
    CASH = "cash"
    BANK = "bank"
    PAYABLE = "payable"
    SETTLEMENT_METHOD_CHOICES = (
        (CASH, "Cash Refund"),
        (BANK, "Bank Refund"),
        (PAYABLE, "Reduce Payable"),
    )

    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="returns")
    supplier = models.ForeignKey(Supplier, null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_returns")
    settlement_method = models.CharField(max_length=20, choices=SETTLEMENT_METHOD_CHOICES, default=PAYABLE)
    bank_account = models.ForeignKey(
        "accounts.BankAccount",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="purchase_returns",
    )
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    reason = models.CharField(max_length=150)
    notes = models.TextField(blank=True)
    voucher = models.ForeignKey(
        "accounts.Voucher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_returns",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="purchase_returns_processed",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Purchase return #{self.id} for purchase #{self.purchase_id}"


class PurchaseReturnItem(models.Model):
    purchase_return = models.ForeignKey(PurchaseReturn, on_delete=models.CASCADE, related_name="items")
    purchase_item = models.ForeignKey(PurchaseItem, on_delete=models.PROTECT, related_name="return_items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="purchase_return_items")
    quantity = models.PositiveIntegerField()
    line_amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"












