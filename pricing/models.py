"""Price lists and schemes.

A price list is a set of trade prices (one per product) that a customer, or every customer
on a route, buys at instead of the product's own sale price. Prices are typed per the largest
pack the product has and kept per base unit, six places, exactly like ``Product.price``.

A scheme (trade offer, bonus) gives something back when a line reaches a quantity: free goods
of the same or another product, a percentage off, or a fixed amount off each unit. Which
scheme a line gets is decided in ``pricing.services.schemes``.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from pos.models import (
    PACK_CARTON,
    PACK_LEVEL_CHOICES,
    PACK_NAMES,
    PACK_SHORT_LABELS,
    format_qty,
    normalize_pack_level,
    quantize_money,
)

ZERO = Decimal("0")


class PriceList(models.Model):
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class PriceListItem(models.Model):
    price_list = models.ForeignKey(PriceList, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("pos.Product", related_name="price_list_items", on_delete=models.CASCADE)
    # Per base unit (one piece), six places, like Product.price.
    price = models.DecimalField(max_digits=16, decimal_places=6)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__name", "product__variant_label", "id"]
        constraints = [
            models.UniqueConstraint(fields=["price_list", "product"], name="pricing_one_price_per_product"),
        ]

    def __str__(self):
        return f"{self.product} on {self.price_list}"

    def pack_price(self, level):
        return quantize_money(Decimal(self.price or 0) * self.product.pack_factor(level))

    @property
    def top_pack_price(self):
        return self.pack_price(self.product.top_pack_level)


class Scheme(models.Model):
    """One trade offer. It applies to a bill line of its product (or of any product in its category)."""

    SCOPE_ALL = "all"
    SCOPE_ROUTE = "route"
    SCOPE_CUSTOMER = "customer"
    SCOPE_CHOICES = (
        (SCOPE_ALL, "Every customer"),
        (SCOPE_ROUTE, "Customers on one route"),
        (SCOPE_CUSTOMER, "One customer"),
    )
    REWARD_FREE = "free"
    REWARD_PERCENT = "percent"
    REWARD_AMOUNT = "amount"
    REWARD_CHOICES = (
        (REWARD_FREE, "Free goods"),
        (REWARD_PERCENT, "Percentage off"),
        (REWARD_AMOUNT, "Amount off each unit"),
    )

    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default=SCOPE_ALL)
    route = models.ForeignKey("pos.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="schemes")
    customer = models.ForeignKey("pos.Customer", null=True, blank=True, on_delete=models.SET_NULL, related_name="schemes")
    # The goods the offer is on: one product, or every product of a category.
    product = models.ForeignKey("pos.Product", null=True, blank=True, on_delete=models.CASCADE, related_name="schemes")
    category = models.ForeignKey("pos.Category", null=True, blank=True, on_delete=models.CASCADE, related_name="schemes")
    # The line must reach this many of this unit: "10" cartons.
    trigger_quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    trigger_unit = models.CharField(max_length=10, choices=PACK_LEVEL_CHOICES, default=PACK_CARTON)
    reward = models.CharField(max_length=10, choices=REWARD_CHOICES, default=REWARD_FREE)
    # Free goods: this many of this unit for every full trigger quantity. No product means the same product.
    free_product = models.ForeignKey(
        "pos.Product", null=True, blank=True, on_delete=models.CASCADE, related_name="free_in_schemes"
    )
    free_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    free_unit = models.CharField(max_length=10, choices=PACK_LEVEL_CHOICES, default=PACK_CARTON)
    # Percentage off the whole line once it reaches the trigger quantity.
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # Rupees off each trigger unit (each carton, say) once the line reaches the trigger quantity.
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-is_active", "name", "id"]
        indexes = [models.Index(fields=["is_active", "start_date", "end_date"], name="pricing_scheme_window_idx")]

    def __str__(self):
        return self.name

    # ------------------------------------------------------------------ reading it
    def is_running(self, day=None):
        day = day or timezone.localdate()
        if not self.is_active:
            return False
        if self.start_date and day < self.start_date:
            return False
        if self.end_date and day > self.end_date:
            return False
        return True

    @property
    def status_label(self):
        today = timezone.localdate()
        if not self.is_active:
            return "Switched off"
        if self.start_date and today < self.start_date:
            return "Not started"
        if self.end_date and today > self.end_date:
            return "Ended"
        return "Running"

    @property
    def goods_label(self):
        if self.product_id:
            return self.product.display_name
        if self.category_id:
            return f"Any {self.category.name}"
        return "-"

    @property
    def scope_label(self):
        if self.scope == self.SCOPE_CUSTOMER:
            return self.customer.name if self.customer_id else "One customer (removed)"
        if self.scope == self.SCOPE_ROUTE:
            return f"Route {self.route.name}" if self.route_id else "One route (removed)"
        return "Every customer"

    @property
    def window_label(self):
        if self.start_date and self.end_date:
            return f"{self.start_date:%d %b %Y} to {self.end_date:%d %b %Y}"
        if self.start_date:
            return f"From {self.start_date:%d %b %Y}"
        if self.end_date:
            return f"Until {self.end_date:%d %b %Y}"
        return "No end date"

    def _unit(self, level, product=None):
        product = product or (self.product if self.product_id else None)
        if product is not None:
            return product.pack_label(level)
        return PACK_SHORT_LABELS.get(normalize_pack_level(level), "pcs")

    @property
    def offer_label(self):
        """"10 Ctn + 1 Ctn free", "5 Ctn or more: 2% off", "5 Ctn or more: 50.00 off a Ctn"."""
        trigger = f"{format_qty(self.trigger_quantity)} {self._unit(self.trigger_unit)}"
        if self.reward == self.REWARD_FREE:
            free_product = self.free_product if self.free_product_id else None
            free = f"{format_qty(self.free_quantity or 0)} {self._unit(self.free_unit, free_product)}"
            other = f" of {free_product.display_name}" if free_product is not None and self.free_product_id != self.product_id else ""
            return f"{trigger} + {free}{other} free"
        if self.reward == self.REWARD_PERCENT:
            return f"{trigger} or more: {Decimal(self.discount_percent or 0).normalize():f}% off"
        return f"{trigger} or more: {quantize_money(self.discount_amount or 0)} off a {self._unit(self.trigger_unit)}"

    # ------------------------------------------------------------------ rules
    def clean(self):
        errors = {}
        self.name = (self.name or "").strip()
        if not self.name:
            errors["name"] = "Give the scheme a name the cashier will recognise, such as 'Surf 10+1 June'."
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors["end_date"] = "The scheme cannot end before it starts."
        if self.scope == self.SCOPE_ROUTE and not self.route_id:
            errors["route"] = "Choose the route."
        if self.scope == self.SCOPE_CUSTOMER and not self.customer_id:
            errors["customer"] = "Choose the customer."
        if self.scope != self.SCOPE_ROUTE:
            self.route = None
        if self.scope != self.SCOPE_CUSTOMER:
            self.customer = None
        if bool(self.product_id) == bool(self.category_id):
            errors["product"] = "Choose one product, or one category for every product in it."
        if Decimal(self.trigger_quantity or 0) <= 0:
            errors["trigger_quantity"] = "Enter how many the line must reach, more than zero."
        self.trigger_unit = normalize_pack_level(self.trigger_unit)
        self.free_unit = normalize_pack_level(self.free_unit)
        if self.reward == self.REWARD_FREE:
            free = Decimal(self.free_quantity or 0)
            if free <= 0:
                errors["free_quantity"] = "Enter how many are given free."
            elif free != free.to_integral_value():
                errors["free_quantity"] = "Free goods are given in whole units."
            # No free product means the same product: for a category scheme, whichever one was bought.
            self.discount_percent = None
            self.discount_amount = None
        elif self.reward == self.REWARD_PERCENT:
            percent = self.discount_percent
            if percent is None or Decimal(percent) <= 0 or Decimal(percent) >= 100:
                errors["discount_percent"] = "Enter a percentage above 0 and below 100."
            self.free_product = None
            self.free_quantity = None
            self.discount_amount = None
        elif self.reward == self.REWARD_AMOUNT:
            amount = self.discount_amount
            if amount is None or Decimal(amount) <= 0:
                errors["discount_amount"] = "Enter the amount taken off each unit."
            self.free_product = None
            self.free_quantity = None
            self.discount_percent = None
        else:
            errors["reward"] = "Choose what the customer gets."
        if errors:
            raise ValidationError(errors)

    @property
    def unit_name(self):
        return PACK_NAMES.get(self.trigger_unit, "Piece")
