from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify


TWO_PLACES = Decimal("0.01")
QTY_PLACES = Decimal("0.001")
ZERO_QTY = Decimal("0")


def quantize_money(value):
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def quantize_qty(value):
    return Decimal(str(value if value not in (None, "") else 0)).quantize(QTY_PLACES, rounding=ROUND_HALF_UP)


def trial_days():
    """Length of the free trial for this install; TRIAL_DAYS in settings, 10 by default."""
    try:
        days = int(getattr(settings, "TRIAL_DAYS", 10))
    except (TypeError, ValueError):
        days = 10
    return max(days, 1)


def format_qty(value):
    """Render a quantity without trailing zeros: 2, 0.5, 1.25."""
    text = format(quantize_qty(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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


class ProductGroup(models.Model):
    """One item sold in several sizes (Bread: Small, Medium, Large). Every size is a full Product row."""

    name = models.CharField(max_length=100)
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_groups",
    )
    brand = models.ForeignKey(
        Brand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_groups",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        super().save(*args, **kwargs)
        # Every size carries the family name, so a line can be named without a join to the group.
        self.variants.exclude(name=self.name).update(name=self.name)


def compose_display_name(name, variant_label="", variant_group_id=None):
    """'Bread - Small' for a size of a grouped item; the plain name for everything else."""
    label = (variant_label or "").strip()
    if variant_group_id and label:
        return f"{name} - {label}"
    return name


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
    UNIT_PIECE = "pcs"
    UNIT_CHOICES = (
        (UNIT_PIECE, "Piece (pcs)"),
        ("box", "Box"),
        ("pack", "Pack"),
        ("dozen", "Dozen"),
        ("kg", "Kilogram (kg)"),
        ("g", "Gram (g)"),
        ("l", "Litre (L)"),
        ("ml", "Millilitre (ml)"),
        ("m", "Metre (m)"),
        ("ft", "Foot (ft)"),
    )
    # Units sold in fractions (0.5 kg); the rest must be whole numbers.
    FRACTIONAL_UNITS = frozenset({"kg", "g", "l", "ml", "m", "ft"})

    name = models.CharField(max_length=100)
    # Sizes of one item: the group is the family ("Bread"), the label is the size ("Small").
    variant_group = models.ForeignKey(
        ProductGroup,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="variants",
    )
    variant_label = models.CharField(max_length=40, blank=True, default="")
    sku = models.CharField(max_length=50, unique=True)
    barcode = models.CharField(max_length=50, unique=True, blank=True, null=True)
    unit = models.CharField(
        max_length=10,
        choices=UNIT_CHOICES,
        default=UNIT_PIECE,
        help_text="Prices, cost and stock are all per this unit.",
    )
    cost_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price = models.DecimalField(max_digits=10, decimal_places=2, help_text="Selling price")
    markup_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Set the sale price as cost plus this percentage. Leave blank to type the price yourself.",
    )
    markup_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Set the sale price as cost plus this fixed amount. Leave blank to type the price yourself.",
    )
    minimum_margin_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    stock = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    track_stock = models.BooleanField(
        default=True,
        help_text="Turn off for services and other items that are never counted on a shelf.",
    )
    low_stock_threshold = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        default=5,
        help_text="Alert when stock falls to this level",
    )
    location = models.CharField(max_length=100, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [
            models.Index(fields=["is_active", "name"], name="pos_product_active_name_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["variant_group", "variant_label"],
                condition=models.Q(variant_group__isnull=False),
                name="pos_product_unique_variant_label",
            ),
        ]

    PRICE_BY_HAND = "manual"
    PRICE_BY_PERCENT = "percent"
    PRICE_BY_AMOUNT = "amount"

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        """The name printed on bills, lists and reports: 'Bread - Small' for a size of a grouped item."""
        return compose_display_name(self.name, self.variant_label, self.variant_group_id)

    @property
    def price_by(self):
        if self.markup_percent is not None:
            return self.PRICE_BY_PERCENT
        if self.markup_amount is not None:
            return self.PRICE_BY_AMOUNT
        return self.PRICE_BY_HAND

    @property
    def has_markup(self):
        return self.markup_percent is not None or self.markup_amount is not None

    @property
    def markup_display(self):
        """'Cost + 25%' or 'Cost + 20.00'; empty when the price is typed by hand."""
        if self.markup_percent is not None:
            return f"Cost + {Decimal(self.markup_percent).normalize():f}%"
        if self.markup_amount is not None:
            return f"Cost + {quantize_money(self.markup_amount)}"
        return ""

    @property
    def allows_fractional_quantity(self):
        return self.unit in self.FRACTIONAL_UNITS

    @property
    def quantity_step(self):
        return "0.001" if self.allows_fractional_quantity else "1"

    @property
    def stock_display(self):
        if not self.track_stock:
            return "Not tracked"
        return f"{format_qty(self.stock)} {self.unit}"

    def validate_quantity(self, quantity):
        """Return an error message when the quantity does not suit this product's unit."""
        quantity = quantize_qty(quantity)
        if quantity <= 0:
            return "Quantity must be greater than zero."
        if not self.allows_fractional_quantity and quantity != quantity.to_integral_value():
            return f"{self.display_name} is sold in whole {self.get_unit_display().lower()} units."
        return ""

    def price_for_cost(self, cost_price=None):
        """Sale price from the markup (a percentage or a fixed amount on cost); None when typed by hand."""
        if self.markup_percent is None and self.markup_amount is None:
            return None
        cost = Decimal(cost_price if cost_price is not None else self.cost_price or 0)
        if self.markup_percent is not None:
            multiplier = Decimal("1") + (Decimal(self.markup_percent) / Decimal("100"))
            return quantize_money(cost * multiplier)
        return quantize_money(cost + Decimal(self.markup_amount))

    def minimum_markup_amount_for_cost(self, cost_price=None):
        """The smallest fixed amount on this cost that still keeps the minimum margin."""
        cost = Decimal(cost_price if cost_price is not None else self.cost_price or 0)
        return quantize_money(self.minimum_sale_price_for_cost(cost) - cost)

    def apply_stock_change(self, delta):
        """Add (or remove) stock against the locked database row, never the stale in-memory value."""
        delta = quantize_qty(delta)
        with transaction.atomic():
            current, tracked = (
                Product.objects.select_for_update().values_list("stock", "track_stock").get(pk=self.pk)
            )
            if not tracked:
                # Services have no shelf quantity: selling or returning one moves nothing.
                self.stock = current
                return current
            new_stock = quantize_qty(current + delta)
            if new_stock < 0:
                raise ValidationError(f"Not enough stock for {self.display_name}.")
            Product.objects.filter(pk=self.pk).update(stock=new_stock)
        self.stock = new_stock
        return new_stock

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
        if self.markup_percent is not None and self.markup_amount is not None:
            errors["markup_amount"] = "Use a percentage or an amount on cost, not both."
        elif self.markup_percent is not None:
            if Decimal(self.markup_percent) < 0:
                errors["markup_percent"] = "Markup cannot be negative."
            elif Decimal(self.markup_percent) < Decimal(self.minimum_margin_percent or 0):
                errors["markup_percent"] = "Markup cannot be lower than the minimum margin."
            elif Decimal(self.cost_price or 0) > 0:
                self.price = self.price_for_cost()
        elif self.markup_amount is not None:
            if Decimal(self.markup_amount) < 0:
                errors["markup_amount"] = "The amount added on cost cannot be negative."
            elif Decimal(self.cost_price or 0) > 0:
                if self.price_for_cost() < self.minimum_sale_price:
                    errors["markup_amount"] = (
                        f"Add at least {self.minimum_markup_amount_for_cost()} on this cost to keep the "
                        f"minimum margin (lowest sale price {self.minimum_sale_price})."
                    )
                else:
                    self.price = self.price_for_cost()

        if "markup_amount" not in errors and Decimal(self.price or 0) and Decimal(self.cost_price or 0):
            minimum_sale_price = self.minimum_sale_price
            if Decimal(self.price) < minimum_sale_price:
                errors["price"] = (
                    f"Sale price must be at least {minimum_sale_price} to keep the minimum margin."
                )

        self.variant_label = (self.variant_label or "").strip()
        if self.variant_group_id and not self.variant_label:
            errors["variant_label"] = "Name this size, such as Small, 500 g or Pack of 6."
        elif self.variant_group_id:
            clash = Product.objects.filter(
                variant_group_id=self.variant_group_id, variant_label__iexact=self.variant_label
            ).exclude(pk=self.pk)
            if clash.exists():
                errors["variant_label"] = f"This item already has a size called {self.variant_label}."

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
    # NULL (not "") for sales made before numbering existed, so the unique index ignores them.
    invoice_number = models.CharField(max_length=40, unique=True, null=True, blank=True, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"], name="pos_sale_created_idx"),
        ]

    def __str__(self):
        return f"Sale {self.display_number}"

    @property
    def display_number(self):
        """Invoice number shown to people; older sales fall back to their id."""
        return self.invoice_number or (f"#{self.pk}" if self.pk else "")

    def save(self, *args, **kwargs):
        if self._state.adding and not self.invoice_number:
            # Same transaction as the insert: a failed sale gives its number back.
            with transaction.atomic():
                self.invoice_number = SystemSettings.allocate_invoice_number()
                return super().save(*args, **kwargs)
        return super().save(*args, **kwargs)

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
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
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
        self.quantity = quantize_qty(self.quantity)
        quantity_error = self.product.validate_quantity(self.quantity)
        if quantity_error:
            errors["quantity"] = quantity_error
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

        if self.product.track_stock:
            available_stock = self.product.stock
            if self.pk:
                old_item = SaleItem.objects.select_related("product").get(pk=self.pk)
                if old_item.product_id == self.product_id:
                    available_stock += old_item.quantity
            if self.quantity > available_stock:
                errors["quantity"] = f"Not enough stock for {self.product.display_name}."

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
        if not self.product.track_stock:
            # Nothing ever entered Inventory for an untracked item, so nothing may leave it as COGS.
            self.cost_price = Decimal("0")
        elif is_new and not self.cost_price:
            self.cost_price = self.product.cost_price

        self.clean()

        with transaction.atomic():
            if is_new:
                self.product.apply_stock_change(-self.quantity)
            else:
                old_item = SaleItem.objects.select_related("product").get(pk=self.pk)
                if old_item.product_id == self.product_id:
                    self.product.apply_stock_change(old_item.quantity - self.quantity)
                else:
                    old_item.product.apply_stock_change(old_item.quantity)
                    self.product.apply_stock_change(-self.quantity)

            super().save(*args, **kwargs)
            self.sale.update_total()

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            self.product.apply_stock_change(self.quantity)
            sale = self.sale
            super().delete(*args, **kwargs)
            sale.update_total()

    def __str__(self):
        return f"{self.product.display_name} x {format_qty(self.quantity)}"


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
    supplier_invoice_number = models.CharField(max_length=50, blank=True)
    bill_date = models.DateField(null=True, blank=True)
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

    class Meta:
        indexes = [
            models.Index(fields=["created_at"], name="pos_purchase_created_idx"),
        ]

    def clean(self):
        errors = {}
        if Decimal(self.paid_amount or 0) < 0:
            errors["paid_amount"] = "Paid amount cannot be negative."
        if self.payment_method == self.CASH or Decimal(self.paid_amount or 0) == 0:
            self.bank_account = None
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
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
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
        elif not self.product.track_stock:
            errors["product"] = (
                f"{self.product.display_name} does not track stock, so it cannot be purchased into inventory. "
                "Record its cost as an expense instead."
            )
        if self.quantity in (None, "") or Decimal(self.quantity) <= 0:
            errors["quantity"] = "Quantity must be greater than zero."
        elif self.product_id:
            self.quantity = quantize_qty(self.quantity)
            quantity_error = self.product.validate_quantity(self.quantity)
            if quantity_error:
                errors["quantity"] = quantity_error
        if self.cost_price in (None, "") or Decimal(self.cost_price or 0) <= 0:
            errors["cost_price"] = "Cost price must be greater than zero."
        priced_from_markup = False
        if (
            self.product_id
            and self.sale_price in (None, "")
            and self.cost_price not in (None, "")
            and self.product.has_markup
        ):
            # Markup-priced products take their sale price from the new cost.
            self.sale_price = self.product.price_for_cost(self.cost_price)
            priced_from_markup = True
        if self.sale_price in (None, "") or Decimal(self.sale_price or 0) <= 0:
            errors["sale_price"] = "Sale price must be greater than zero."

        if self.product_id and self.cost_price not in (None, "") and self.sale_price not in (None, ""):
            minimum_sale_price = self.product.minimum_sale_price_for_cost(self.cost_price)
            if Decimal(self.sale_price or 0) < minimum_sale_price:
                if priced_from_markup:
                    errors["sale_price"] = (
                        f"{self.product.markup_display} on this cost gives {quantize_money(self.sale_price)}, "
                        f"below the lowest price {minimum_sale_price} for the minimum margin. "
                        "Type a sale price, or raise the markup on the product."
                    )
                else:
                    errors["sale_price"] = (
                        f"Sale price must be at least {minimum_sale_price} for the configured minimum margin."
                    )

        if errors:
            raise ValidationError(errors)

    def _average_cost(self, stock_before, old_quantity=ZERO_QTY, old_cost=Decimal("0"), current_cost=None):
        """Weighted-average cost after this line, so COGS follows what was actually paid."""
        if current_cost is None:
            current_cost = self.product.cost_price
        current_cost = Decimal(current_cost or 0)
        stock_before = max(Decimal(stock_before), ZERO_QTY)
        value = (stock_before * current_cost) - (Decimal(old_quantity) * Decimal(old_cost)) + (
            Decimal(self.quantity) * Decimal(self.cost_price)
        )
        units = stock_before - Decimal(old_quantity) + Decimal(self.quantity)
        if units <= 0 or value <= 0 or current_cost <= 0:
            return quantize_money(self.cost_price)
        return quantize_money(value / units)

    def _sync_product_pricing(self, average_cost):
        updated_fields = []
        if self.purchase.supplier_id and self.product.supplier_id != self.purchase.supplier_id:
            self.product.supplier = self.purchase.supplier
            updated_fields.append("supplier")
        if Decimal(self.product.cost_price or 0) != average_cost:
            self.product.cost_price = average_cost
            updated_fields.append("cost_price")
        if Decimal(self.product.price or 0) != Decimal(self.sale_price or 0):
            self.product.price = self.sale_price
            updated_fields.append("price")

        if updated_fields:
            self.product.save(update_fields=updated_fields)

    def save(self, *args, **kwargs):
        self.clean()
        is_new = self.pk is None

        with transaction.atomic():
            # Cost comes from the same locked row as the stock: a second line for the same
            # product must average against what the first line just wrote, not a stale copy.
            stock_before, cost_before = (
                Product.objects.select_for_update().values_list("stock", "cost_price").get(pk=self.product_id)
            )
            self.product.cost_price = cost_before
            pricing_changed = is_new
            average_cost = None

            if is_new:
                average_cost = self._average_cost(stock_before)
                self.product.apply_stock_change(self.quantity)
            else:
                old_item = PurchaseItem.objects.select_related("product").get(pk=self.pk)
                pricing_changed = (
                    old_item.product_id != self.product_id
                    or Decimal(old_item.cost_price) != Decimal(self.cost_price)
                    or Decimal(old_item.sale_price or 0) != Decimal(self.sale_price or 0)
                    or Decimal(old_item.quantity) != Decimal(self.quantity)
                )
                returned = sum((row.quantity for row in self.return_items.all()), ZERO_QTY)
                if Decimal(self.quantity) < returned:
                    raise ValidationError(
                        f"{format_qty(returned)} of {self.product.display_name} was already returned to the supplier."
                    )

                if old_item.product_id == self.product_id:
                    average_cost = self._average_cost(stock_before, old_item.quantity, old_item.cost_price)
                    try:
                        self.product.apply_stock_change(Decimal(self.quantity) - Decimal(old_item.quantity))
                    except ValidationError as exc:
                        raise ValidationError("Cannot reduce stock below zero") from exc
                else:
                    try:
                        old_item.product.apply_stock_change(-old_item.quantity)
                    except ValidationError as exc:
                        raise ValidationError(
                            "Cannot change this purchase item because existing stock has already been consumed"
                        ) from exc
                    average_cost = self._average_cost(stock_before)
                    self.product.apply_stock_change(self.quantity)

            super().save(*args, **kwargs)
            # Re-saving an untouched line (e.g. editing only the paid amount of an old
            # purchase) must not push old prices back onto the product.
            if pricing_changed:
                self._sync_product_pricing(average_cost)
            self.purchase.update_total()

    def delete(self, *args, **kwargs):
        if self.pk and self.return_items.exists():
            raise ValidationError(
                f"{self.product.display_name} has supplier returns recorded against it and cannot be removed."
            )
        with transaction.atomic():
            try:
                self.product.apply_stock_change(-self.quantity)
            except ValidationError as exc:
                raise ValidationError("Cannot reduce stock below zero") from exc

            purchase = self.purchase
            super().delete(*args, **kwargs)
            purchase.update_total()

    def __str__(self):
        return f"{self.product.display_name} x {format_qty(self.quantity)}"


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
        indexes = [
            models.Index(fields=["user", "login_at"], name="pos_sessionlog_user_login_idx"),
        ]

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




class UserPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="preference")
    time_zone = models.CharField(max_length=64, blank=True, default="")
    # Google's stable account id; an email address can be reassigned, this cannot.
    google_sub = models.CharField(max_length=255, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Preference"
        verbose_name_plural = "User Preferences"

    def __str__(self):
        return f"Preferences for {self.user.get_username()}"

    @property
    def uses_system_detected_time_zone(self):
        return not bool((self.time_zone or "").strip())
class OwnerOnboarding(models.Model):
    PLAN_STARTER = "starter"
    PLAN_GROWTH = "growth"
    PLAN_SCALE = "scale"
    PLAN_CHOICES = (
        (PLAN_STARTER, "Starter"),
        (PLAN_GROWTH, "Growth"),
        (PLAN_SCALE, "Scale"),
    )

    BILLING_TRIAL = "trial"
    BILLING_PENDING = "pending"
    BILLING_ACTIVE = "active"
    BILLING_CHOICES = (
        (BILLING_TRIAL, "Trial"),
        (BILLING_PENDING, "Pending Checkout"),
        (BILLING_ACTIVE, "Active Subscription"),
    )

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="owner_onboarding")
    selected_plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default=PLAN_STARTER)
    billing_state = models.CharField(max_length=20, choices=BILLING_CHOICES, default=BILLING_TRIAL)
    email_verified = models.BooleanField(default=False)
    email_verified_at = models.DateTimeField(null=True, blank=True)
    verification_sent_at = models.DateTimeField(null=True, blank=True)
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    business_created_at = models.DateTimeField(null=True, blank=True)
    # High-water mark of the system clock. An offline install cannot ask a server what
    # time it is, so this is what stops "set the date back" from extending the trial.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    CLOCK_ROLLBACK_TOLERANCE = timedelta(days=1)
    LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=10)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Owner onboarding for {self.user.email or self.user.get_username()}"

    @property
    def clock_rolled_back(self):
        if not self.last_seen_at:
            return False
        return timezone.now() < self.last_seen_at - self.CLOCK_ROLLBACK_TOLERANCE

    @property
    def trusted_now(self):
        """The clock, but never earlier than the latest moment this install has already seen."""
        now = timezone.now()
        if self.last_seen_at and self.last_seen_at > now:
            return self.last_seen_at
        return now

    def touch_last_seen(self, now=None):
        """Advance the high-water mark. It never moves backwards and is written at most every few minutes."""
        if not self.pk:
            return
        now = now or timezone.now()
        if self.last_seen_at and now < self.last_seen_at + self.LAST_SEEN_WRITE_INTERVAL:
            return
        OwnerOnboarding.objects.filter(pk=self.pk).filter(
            models.Q(last_seen_at__isnull=True) | models.Q(last_seen_at__lt=now)
        ).update(last_seen_at=now)
        self.last_seen_at = now

    @property
    def plan_label(self):
        return self.get_selected_plan_display()

    @property
    def current_subscription(self):
        return getattr(self, "billing_subscription", None)

    @property
    def trial_is_active(self):
        if not self.trial_started_at or not self.trial_ends_at:
            return False
        if self.clock_rolled_back:
            return False
        return self.trusted_now <= self.trial_ends_at

    @property
    def trial_days_remaining(self):
        if not self.trial_ends_at or self.clock_rolled_back:
            return 0
        remaining = self.trial_ends_at - self.trusted_now
        if remaining.total_seconds() <= 0:
            return 0
        return remaining.days + (1 if remaining.seconds else 0)

    @property
    def trial_length_days(self):
        if self.trial_started_at and self.trial_ends_at:
            return max((self.trial_ends_at - self.trial_started_at).days, 1)
        return trial_days()

    @property
    def subscription_is_active(self):
        subscription = self.current_subscription
        return bool(subscription and subscription.grants_access)

    @property
    def active_license(self):
        from pos.services.licensing import get_active_license

        return get_active_license(now=self.trusted_now)

    @property
    def license_is_active(self):
        return self.active_license is not None

    @property
    def billing_is_active(self):
        """The one rule for workspace access: trial, OR paid subscription, OR valid licence key."""
        return self.trial_is_active or self.subscription_is_active or self.license_is_active

    @property
    def requires_payment_setup(self):
        return bool(self.email_verified and self.business_created_at and not self.billing_is_active)

    @property
    def billing_status_label(self):
        subscription = self.current_subscription
        active_license = self.active_license
        if active_license is not None:
            return active_license.status_label
        if subscription is not None and subscription.grants_access:
            return subscription.status_label
        if self.trial_is_active:
            days = self.trial_days_remaining
            return f"Free trial active ({days} day{'s' if days != 1 else ''} left)"
        if self.clock_rolled_back:
            return "Check the computer date and time"
        if subscription is not None:
            return subscription.status_label
        if self.email_verified and self.business_created_at:
            return "Activation required"
        return "Setup in progress"

    def mark_verification_sent(self, now=None):
        self.verification_sent_at = now or timezone.now()
        self.save(update_fields=["verification_sent_at"])

    def mark_email_verified(self, now=None):
        now = now or timezone.now()
        self.email_verified = True
        self.email_verified_at = now
        self.save(update_fields=["email_verified", "email_verified_at"])

    def start_trial(self, now=None):
        now = now or timezone.now()
        update_fields = []
        if self.trial_started_at is None:
            self.trial_started_at = now
            update_fields.append("trial_started_at")
        if self.trial_ends_at is None:
            self.trial_ends_at = now + timedelta(days=trial_days())
            update_fields.append("trial_ends_at")
        if self.last_seen_at is None:
            self.last_seen_at = now
            update_fields.append("last_seen_at")
        if self.billing_state == self.BILLING_PENDING:
            self.billing_state = self.BILLING_TRIAL
            update_fields.append("billing_state")
        if update_fields:
            self.save(update_fields=update_fields)

    def mark_checkout_started(self, plan_code="", now=None):
        now = now or timezone.now()
        update_fields = []
        normalized_plan = (plan_code or self.selected_plan or self.PLAN_STARTER).strip().lower()
        allowed_plans = {choice for choice, _ in self.PLAN_CHOICES}
        if normalized_plan in allowed_plans and normalized_plan != self.selected_plan:
            self.selected_plan = normalized_plan
            update_fields.append("selected_plan")
        if not self.billing_is_active and self.billing_state != self.BILLING_PENDING:
            self.billing_state = self.BILLING_PENDING
            update_fields.append("billing_state")
        if update_fields:
            self.save(update_fields=update_fields)
        return now

    def sync_billing_state(self):
        subscription = self.current_subscription
        target_state = self.BILLING_PENDING
        if (subscription and subscription.grants_access) or self.license_is_active:
            target_state = self.BILLING_ACTIVE
        elif self.trial_is_active:
            target_state = self.BILLING_TRIAL
        if self.billing_state != target_state:
            self.billing_state = target_state
            self.save(update_fields=["billing_state"])
        return target_state

    def mark_business_created(self, now=None):
        self.business_created_at = now or timezone.now()
        self.save(update_fields=["business_created_at"])
        self.sync_billing_state()


class BillingSubscription(models.Model):
    PROVIDER_LEMON_SQUEEZY = "lemonsqueezy"
    PROVIDER_CHOICES = (
        (PROVIDER_LEMON_SQUEEZY, "Lemon Squeezy"),
    )

    STATUS_PENDING = "pending"
    STATUS_ON_TRIAL = "on_trial"
    STATUS_ACTIVE = "active"
    STATUS_PAST_DUE = "past_due"
    STATUS_CANCELLED = "cancelled"
    STATUS_PAUSED = "paused"
    STATUS_EXPIRED = "expired"
    STATUS_UNPAID = "unpaid"
    STATUS_REFUNDED = "refunded"
    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending Checkout"),
        (STATUS_ON_TRIAL, "Provider Trial"),
        (STATUS_ACTIVE, "Active"),
        (STATUS_PAST_DUE, "Past Due"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_PAUSED, "Paused"),
        (STATUS_EXPIRED, "Expired"),
        (STATUS_UNPAID, "Unpaid"),
        (STATUS_REFUNDED, "Refunded"),
    )

    owner_onboarding = models.OneToOneField(OwnerOnboarding, on_delete=models.CASCADE, related_name="billing_subscription")
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES, default=PROVIDER_LEMON_SQUEEZY)
    plan_code = models.CharField(max_length=20, choices=OwnerOnboarding.PLAN_CHOICES, default=OwnerOnboarding.PLAN_STARTER)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    checkout_url = models.URLField(blank=True)
    customer_portal_url = models.URLField(blank=True)
    provider_customer_id = models.CharField(max_length=80, blank=True)
    provider_order_id = models.CharField(max_length=80, blank=True)
    provider_subscription_id = models.CharField(max_length=80, blank=True, db_index=True)
    provider_product_id = models.CharField(max_length=80, blank=True)
    provider_variant_id = models.CharField(max_length=80, blank=True)
    product_name = models.CharField(max_length=120, blank=True)
    variant_name = models.CharField(max_length=120, blank=True)
    checkout_started_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    renews_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    paused_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    last_event_name = models.CharField(max_length=120, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    # The provider's own updated_at from the last applied webhook; older deliveries are ignored.
    provider_updated_at = models.DateTimeField(null=True, blank=True)
    past_due_since = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    PAST_DUE_GRACE = timedelta(days=7)

    class Meta:
        ordering = ["-updated_at", "-created_at"]

    def __str__(self):
        return f"{self.get_provider_display()} subscription for {self.owner_onboarding.user.get_username()}"

    @property
    def plan_label(self):
        labels = dict(OwnerOnboarding.PLAN_CHOICES)
        return labels.get(self.plan_code, self.plan_code.replace("_", " ").title())

    @property
    def status_label(self):
        labels = dict(self.STATUS_CHOICES)
        return labels.get(self.status, self.status.replace("_", " ").title())

    @property
    def status_tone(self):
        if self.grants_access:
            return "healthy"
        if self.status in {self.STATUS_PENDING, self.STATUS_CANCELLED, self.STATUS_PAST_DUE}:
            return "advance"
        return "payable"

    @property
    def past_due_grace_ends_at(self):
        if self.status != self.STATUS_PAST_DUE:
            return None
        started = self.past_due_since or self.last_event_at or self.updated_at or timezone.now()
        return started + self.PAST_DUE_GRACE

    @property
    def grants_access(self):
        now = timezone.now()
        if self.status in {self.STATUS_ACTIVE, self.STATUS_ON_TRIAL}:
            return True
        if self.status == self.STATUS_PAST_DUE:
            # A failed renewal keeps the shop open for a bounded grace period, not forever.
            return now <= self.past_due_grace_ends_at
        if self.status == self.STATUS_CANCELLED:
            # Cancelled keeps access only up to a known paid-through date.
            return self.ends_at is not None and now <= self.ends_at
        return False


class BillingWebhookEvent(models.Model):
    provider = models.CharField(max_length=20, choices=BillingSubscription.PROVIDER_CHOICES, default=BillingSubscription.PROVIDER_LEMON_SQUEEZY)
    provider_event_id = models.CharField(max_length=120, blank=True, db_index=True)
    event_name = models.CharField(max_length=120, blank=True)
    event_hash = models.CharField(max_length=64, unique=True)
    signature = models.CharField(max_length=255, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-processed_at"]

    def __str__(self):
        return f"{self.get_provider_display()} {self.event_name or 'webhook'}"


class License(models.Model):
    """An activated offline licence key.

    The parsed columns are for display and reporting only. Access decisions always
    re-verify the signed key text, so editing a row in the database unlocks nothing.
    """

    key = models.TextField()
    key_hash = models.CharField(max_length=64, unique=True)
    license_id = models.CharField(max_length=64, blank=True)
    licensee = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    plan = models.CharField(max_length=40, blank=True)
    # Display copies of the signed payload. modules=None means every module (Enterprise, or an older key).
    edition = models.CharField(max_length=40, blank=True)
    modules = models.JSONField(null=True, blank=True)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True, blank=True)
    max_users = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    activated_at = models.DateTimeField(default=timezone.now)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="activated_licenses",
    )

    class Meta:
        ordering = ["-activated_at", "-id"]

    def __str__(self):
        return f"Licence for {self.licensee}"

    @property
    def is_perpetual(self):
        return self.expires_at is None

    def is_expired(self, now=None):
        if self.expires_at is None:
            return False
        return (now or timezone.now()) > self.expires_at

    @property
    def days_remaining(self):
        if self.expires_at is None:
            return None
        remaining = self.expires_at - timezone.now()
        if remaining.total_seconds() <= 0:
            return 0
        return remaining.days + (1 if remaining.seconds else 0)

    @property
    def plan_label(self):
        return (self.plan or "Licensed").replace("_", " ").title()

    @property
    def status_label(self):
        if self.is_perpetual:
            return "Licensed (lifetime)"
        if self.is_expired():
            return "Licence expired"
        days = self.days_remaining
        return f"Licensed ({days} day{'s' if days != 1 else ''} left)"

    @property
    def max_users_label(self):
        return "Unlimited" if self.max_users is None else str(self.max_users)

    @property
    def edition_label(self):
        from pos.services.modules import edition_label

        return edition_label(self.edition) or ("Enterprise" if self.modules is None else "Custom")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from pos.services.modules import invalidate_module_state

        invalidate_module_state()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        from pos.services.modules import invalidate_module_state

        invalidate_module_state()
        return result


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
    allow_zero_total_sales = models.BooleanField(
        default=False,
        help_text="Let a sale close at a total of zero (a 100% discount or a giveaway).",
    )
    default_sale_payment_method = models.CharField(
        max_length=10,
        choices=Sale.PAYMENT_METHOD_CHOICES,
        default=Sale.CASH,
    )
    thermal_printer_width_mm = models.PositiveIntegerField(default=50)
    currency_symbol = models.CharField(max_length=8, default="Rs", help_text="Shown before every amount.")
    currency_code = models.CharField(max_length=3, default="PKR", help_text="ISO code, e.g. PKR, USD, AED.")
    default_markup_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Suggested markup on cost for new products.",
    )
    invoice_prefix = models.CharField(
        max_length=12,
        default="INV-",
        blank=True,
        help_text="Printed before the invoice number, e.g. INV-000125.",
    )
    next_invoice_sequence = models.PositiveIntegerField(default=1, editable=False)
    # Feature modules the owner switched off (keys of pos.services.modules). The licence
    # sets the ceiling; this list only ever narrows it.
    disabled_modules = models.JSONField(default=list, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    INVOICE_NUMBER_DIGITS = 6

    class Meta:
        verbose_name = "System Settings"
        verbose_name_plural = "System Settings"

    def __str__(self):
        return "System Settings"

    @classmethod
    def load_raw(cls):
        """The row exactly as stored. Only the module switches need this."""
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings

    @classmethod
    def load(cls):
        """The settings in force: switches of modules the shop cannot use read as off."""
        from pos.services.modules import apply_module_mask

        return apply_module_mask(cls.load_raw())

    @classmethod
    def allocate_invoice_number(cls):
        """Hand out the next invoice number. Call inside the transaction that creates the sale."""
        cls.load()
        with transaction.atomic():
            row = cls.objects.select_for_update().get(pk=1)
            prefix = (row.invoice_prefix or "").strip()
            sequence = max(int(row.next_invoice_sequence or 1), 1)
            number = f"{prefix}{sequence:0{cls.INVOICE_NUMBER_DIGITS}d}"
            # The settings form saves the whole row, so a stale counter can come back; skip past used numbers.
            while Sale.objects.filter(invoice_number=number).exists():
                sequence += 1
                number = f"{prefix}{sequence:0{cls.INVOICE_NUMBER_DIGITS}d}"
            cls.objects.filter(pk=1).update(next_invoice_sequence=sequence + 1)
        return number

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
        self.invoice_prefix = (self.invoice_prefix or "").strip()
        self.thermal_printer_width_mm = max(int(self.thermal_printer_width_mm or 50), 50)
        self.expiry_alert_days = max(int(self.expiry_alert_days or 30), 1)
        self.cash_gst_rate = quantize_money(max(Decimal(self.cash_gst_rate or 0), Decimal("0")))
        self.bank_gst_rate = quantize_money(max(Decimal(self.bank_gst_rate or 0), Decimal("0")))
        # load() masks the switches of unavailable modules; the owner's stored choice must survive a save.
        masked = [flag for flag in (getattr(self, "_masked_flags", None) or {}) if getattr(self, flag) is False]
        for flag in masked:
            setattr(self, flag, True)
        try:
            super().save(*args, **kwargs)
        finally:
            for flag in masked:
                setattr(self, flag, False)
        from pos.services.modules import invalidate_module_state

        invalidate_module_state()

class StockAdjustment(models.Model):
    ADJUSTMENT = "adjustment"
    STOCK_TAKE = "stock_take"
    OPENING_STOCK = "opening_stock"
    MODE_CHOICES = (
        (ADJUSTMENT, "Stock Adjustment"),
        (STOCK_TAKE, "Stock Take"),
        (OPENING_STOCK, "Opening Stock"),
    )

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_adjustments")
    adjustment_mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=ADJUSTMENT)
    quantity_before = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantity_after = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    quantity_delta = models.DecimalField(max_digits=12, decimal_places=3, default=0)
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
        indexes = [
            models.Index(fields=["product", "created_at"], name="pos_stockadj_prod_created_idx"),
        ]

    def __str__(self):
        return f"Stock adjustment #{self.id} - {self.product.display_name}"


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
        indexes = [
            models.Index(fields=["created_at"], name="pos_salereturn_created_idx"),
        ]

    def __str__(self):
        return f"Sale return #{self.id} for sale {self.sale.display_number}"


class SaleReturnItem(models.Model):
    sale_return = models.ForeignKey(SaleReturn, on_delete=models.CASCADE, related_name="items")
    sale_item = models.ForeignKey(SaleItem, on_delete=models.PROTECT, related_name="return_items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="sale_return_items")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_amount = models.DecimalField(max_digits=12, decimal_places=2)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.display_name} x {format_qty(self.quantity)}"


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
        indexes = [
            models.Index(fields=["created_at"], name="pos_purchreturn_created_idx"),
        ]

    def __str__(self):
        return f"Purchase return #{self.id} for purchase #{self.purchase_id}"


class PurchaseReturnItem(models.Model):
    purchase_return = models.ForeignKey(PurchaseReturn, on_delete=models.CASCADE, related_name="items")
    purchase_item = models.ForeignKey(PurchaseItem, on_delete=models.PROTECT, related_name="return_items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="purchase_return_items")
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    line_amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.display_name} x {format_qty(self.quantity)}"












