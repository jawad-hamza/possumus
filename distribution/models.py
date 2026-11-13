"""The distributor's working day: orders booked on routes, the load that goes out, what comes
back, and the money recovered.

These records sit on top of the shop core in ``pos`` and ``accounts``. An order becomes an
ordinary sale invoice; a load form is a list of invoices going out together; returns,
stock returns and recoveries post to the same ledgers every other entry uses. Quantities
follow the packaging rule of ``pos``: a line keeps the unit it was typed in (carton, box or
piece) and the number of base units one of them held (``unit_factor``).
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from pos.models import PACK_LEVEL_CHOICES, PACK_PIECE, format_qty, quantize_money, quantize_qty


ZERO = Decimal("0")


# ------------------------------------------------------------------------------ masters
class DeliveryMan(models.Model):
    """The person who takes a load out, hands over the goods and brings back cash and returns."""

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]
        verbose_name_plural = "Delivery men"

    def __str__(self):
        return self.name


class Vehicle(models.Model):
    name = models.CharField(max_length=100)
    number = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return f"{self.name} ({self.number})" if self.number else self.name


class PackLineMixin(models.Model):
    """A line of goods in the unit it was typed in."""

    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_level = models.CharField(max_length=10, choices=PACK_LEVEL_CHOICES, default=PACK_PIECE)
    unit_factor = models.DecimalField(max_digits=12, decimal_places=3, default=1)

    class Meta:
        abstract = True

    @property
    def base_quantity(self):
        return quantize_qty(Decimal(self.quantity or 0) * Decimal(self.unit_factor or 1))

    @property
    def unit_label(self):
        return self.product.pack_label(self.unit_level)

    @property
    def quantity_display(self):
        return f"{format_qty(self.quantity)} {self.unit_label}"

    def resolve_unit(self):
        """Fix the level to one the product has and take its size from the product."""
        self.unit_level = self.product.resolve_pack_level(self.unit_level)
        self.unit_factor = self.product.pack_factor(self.unit_level)


# ------------------------------------------------------------------------------- orders
class SalesOrder(models.Model):
    """What an order booker wrote down at a shop. It moves no stock and no money until it is invoiced."""

    BOOKED = "booked"
    INVOICED = "invoiced"
    CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (BOOKED, "Booked"),
        (INVOICED, "Invoiced"),
        (CANCELLED, "Cancelled"),
    )

    customer = models.ForeignKey("pos.Customer", on_delete=models.PROTECT, related_name="sales_orders")
    order_booker = models.ForeignKey(
        "pos.OrderBooker", null=True, blank=True, on_delete=models.SET_NULL, related_name="sales_orders"
    )
    route = models.ForeignKey("pos.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="sales_orders")
    order_date = models.DateField(default=timezone.localdate)
    delivery_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=BOOKED)
    notes = models.CharField(max_length=255, blank=True)
    # The invoice this order became. One order, one invoice; what could not be supplied stays
    # on the order lines as the short quantity.
    sale = models.OneToOneField("pos.Sale", null=True, blank=True, on_delete=models.SET_NULL, related_name="sales_order")
    invoiced_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=150, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="sales_orders_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-order_date", "-id"]
        indexes = [models.Index(fields=["status", "order_date"], name="dist_order_status_date_idx")]

    def __str__(self):
        return f"Order {self.display_number}"

    @property
    def display_number(self):
        return f"SO-{self.pk:06d}" if self.pk else ""

    @property
    def total_amount(self):
        return quantize_money(sum((item.line_total for item in self.items.all()), ZERO))

    @property
    def invoiced_amount(self):
        return quantize_money(sum((item.invoiced_total for item in self.items.all()), ZERO))

    @property
    def short_amount(self):
        if self.status != self.INVOICED:
            return ZERO
        return quantize_money(self.total_amount - self.invoiced_amount)

    @property
    def has_shortage(self):
        return self.status == self.INVOICED and any(item.short_quantity > 0 for item in self.items.all())

    @property
    def is_open(self):
        return self.status == self.BOOKED


class SalesOrderItem(PackLineMixin):
    order = models.ForeignKey(SalesOrder, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("pos.Product", on_delete=models.PROTECT, related_name="sales_order_items")
    # Rate per the line's unit when the order was booked.
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # What actually went on the invoice, in the line's unit. Empty until the order is invoiced.
    invoiced_quantity = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.display_name} x {self.quantity_display}"

    @property
    def discounted_unit_price(self):
        multiplier = Decimal("1") - (Decimal(self.discount_percent or 0) / Decimal("100"))
        return quantize_money(Decimal(self.price or 0) * multiplier)

    @property
    def line_total(self):
        return quantize_money(self.discounted_unit_price * Decimal(self.quantity or 0))

    @property
    def invoiced_total(self):
        return quantize_money(self.discounted_unit_price * Decimal(self.invoiced_quantity or 0))

    @property
    def short_quantity(self):
        """Ordered but not supplied, in the line's unit. Zero until the order is invoiced."""
        if self.invoiced_quantity is None:
            return ZERO
        return max(quantize_qty(Decimal(self.quantity) - Decimal(self.invoiced_quantity)), ZERO)


# ---------------------------------------------------------------------------- load form
class LoadForm(models.Model):
    """The invoices that leave together on one vehicle, and what came back at the end of the day."""

    OPEN = "open"
    DISPATCHED = "dispatched"
    SETTLED = "settled"
    STATUS_CHOICES = (
        (OPEN, "Being loaded"),
        (DISPATCHED, "Out for delivery"),
        (SETTLED, "Settled"),
    )

    load_date = models.DateField(default=timezone.localdate)
    route = models.ForeignKey("pos.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="load_forms")
    delivery_man = models.ForeignKey(
        DeliveryMan, null=True, blank=True, on_delete=models.SET_NULL, related_name="load_forms"
    )
    vehicle = models.ForeignKey(Vehicle, null=True, blank=True, on_delete=models.SET_NULL, related_name="load_forms")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=OPEN)
    notes = models.CharField(max_length=255, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    # Day-end settlement: the cash the delivery man should hand in, what he did hand in, and the
    # difference (received less expected; below zero is a shortage). Fixed when the load is closed.
    cash_expected = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_received = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_variance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    variance_voucher = models.ForeignKey(
        "accounts.Voucher", null=True, blank=True, on_delete=models.SET_NULL, related_name="load_forms"
    )
    settlement_notes = models.CharField(max_length=255, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="load_forms_settled"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="load_forms_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-load_date", "-id"]
        indexes = [models.Index(fields=["status", "load_date"], name="dist_load_status_date_idx")]

    def __str__(self):
        return f"Load {self.display_number}"

    @property
    def display_number(self):
        return f"LF-{self.pk:06d}" if self.pk else ""

    @property
    def is_open(self):
        return self.status == self.OPEN

    @property
    def is_dispatched(self):
        return self.status == self.DISPATCHED

    @property
    def is_settled(self):
        return self.status == self.SETTLED


class LoadFormInvoice(models.Model):
    """One invoice on a load. An invoice rides on at most one load."""

    load_form = models.ForeignKey(LoadForm, related_name="invoices", on_delete=models.CASCADE)
    sale = models.OneToOneField("pos.Sale", on_delete=models.PROTECT, related_name="load_entry")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.sale.display_number} on {self.load_form.display_number}"


class LoadFormReturn(models.Model):
    """A sale return posted at the day-end settlement of a load: goods that came back on the van."""

    load_form = models.ForeignKey(LoadForm, related_name="sale_returns", on_delete=models.CASCADE)
    sale_return = models.OneToOneField("pos.SaleReturn", on_delete=models.PROTECT, related_name="load_entry")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


# -------------------------------------------------------------------------- stock return
class StockReturn(models.Model):
    """Goods coming back from a customer, or going back to a supplier, without naming one invoice.

    Returns against a single invoice or purchase bill keep using the return buttons on those
    documents (and the day-end settlement of a load); this is for market returns and for
    sending stock back to the company.
    """

    FROM_CUSTOMER = "customer"
    TO_SUPPLIER = "supplier"
    KIND_CHOICES = (
        (FROM_CUSTOMER, "Market return (from a customer)"),
        (TO_SUPPLIER, "Return to supplier"),
    )
    ON_ACCOUNT = "account"
    CASH = "cash"
    BANK = "bank"
    SETTLEMENT_CHOICES = (
        (ON_ACCOUNT, "Adjust their account"),
        (CASH, "Cash"),
        (BANK, "Bank"),
    )

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    customer = models.ForeignKey(
        "pos.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_returns"
    )
    supplier = models.ForeignKey(
        "pos.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_returns"
    )
    return_date = models.DateField(default=timezone.localdate)
    settlement = models.CharField(max_length=10, choices=SETTLEMENT_CHOICES, default=ON_ACCOUNT)
    bank_account = models.ForeignKey(
        "accounts.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="stock_returns"
    )
    reason = models.CharField(max_length=150)
    notes = models.CharField(max_length=255, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    voucher = models.ForeignKey(
        "accounts.Voucher", null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_returns"
    )
    # Set when the goods came back with a load.
    load_form = models.ForeignKey(
        LoadForm, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_returns"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_returns_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-return_date", "-id"]
        indexes = [models.Index(fields=["kind", "return_date"], name="dist_return_kind_date_idx")]

    def __str__(self):
        return f"Stock return {self.display_number}"

    @property
    def display_number(self):
        return f"SR-{self.pk:06d}" if self.pk else ""

    @property
    def party(self):
        return self.customer if self.kind == self.FROM_CUSTOMER else self.supplier

    @property
    def party_name(self):
        party = self.party
        if party is not None:
            return party.name
        return "Walk-in customer" if self.kind == self.FROM_CUSTOMER else "No supplier"


class StockReturnItem(PackLineMixin):
    SALEABLE = "saleable"
    DAMAGED = "damaged"
    CONDITION_CHOICES = (
        (SALEABLE, "Good"),
        (DAMAGED, "Damaged or expired"),
    )

    stock_return = models.ForeignKey(StockReturn, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey("pos.Product", on_delete=models.PROTECT, related_name="stock_return_items")
    # Per the line's unit: what the customer is credited, or what the supplier is charged.
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    line_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Stock value of the goods at average cost: what Inventory (or Damaged Stock) moves by.
    cost_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES, default=SALEABLE)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.product.display_name} x {self.quantity_display}"


# ------------------------------------------------------------------------------ recovery
class Recovery(models.Model):
    """Money collected from a customer against what they owe, and who brought it in.

    The money itself is an ordinary customer receipt (``accounts.Settlement``); this row adds
    the route, the collector and the load it came back with, for the recovery reports.
    """

    BY_ORDER_BOOKER = "order_booker"
    BY_DELIVERY_MAN = "delivery_man"
    BY_OFFICE = "office"
    COLLECTOR_CHOICES = (
        (BY_ORDER_BOOKER, "Order booker"),
        (BY_DELIVERY_MAN, "Delivery man"),
        (BY_OFFICE, "Recovery man or office"),
    )
    CASH = "cash"
    BANK = "bank"
    PAYMENT_METHOD_CHOICES = ((CASH, "Cash"), (BANK, "Bank"))

    settlement = models.OneToOneField("accounts.Settlement", on_delete=models.CASCADE, related_name="recovery")
    customer = models.ForeignKey("pos.Customer", on_delete=models.PROTECT, related_name="recoveries")
    # Where the customer stood when the money came in: their route and their order booker.
    route = models.ForeignKey("pos.Route", null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries")
    order_booker = models.ForeignKey(
        "pos.OrderBooker", null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries"
    )
    # Who brought the money in.
    collected_by = models.CharField(max_length=15, choices=COLLECTOR_CHOICES, default=BY_OFFICE)
    collector_booker = models.ForeignKey(
        "pos.OrderBooker", null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries_collected"
    )
    delivery_man = models.ForeignKey(
        DeliveryMan, null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries"
    )
    collector_name = models.CharField(max_length=100, blank=True)
    load_form = models.ForeignKey(
        LoadForm, null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries"
    )
    # The invoice the money was paid against, when the customer named one. The ledger is kept
    # per customer, so this is a note for the reports; the money always reduces the account.
    sale = models.ForeignKey("pos.Sale", null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    collected_on = models.DateField(default=timezone.localdate)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="recoveries_recorded"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-collected_on", "-id"]
        verbose_name_plural = "Recoveries"
        indexes = [models.Index(fields=["collected_on"], name="dist_recovery_date_idx")]

    def __str__(self):
        return f"Recovery of {self.amount} from {self.customer}"

    @property
    def collector_label(self):
        return self.collector_name or self.get_collected_by_display()
