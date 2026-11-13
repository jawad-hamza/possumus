"""Which scheme a bill line gets, and what it gives.

A scheme APPLIES to a line when all of these hold:

* it is switched on and today is inside its start and end dates (either may be open);
* its scope matches: every customer, the customer's route, or that one customer (a walk-in sale
  only gets schemes for every customer);
* it is on the line's product, or on the line's category;
* the line reaches the trigger quantity. The trigger is counted in base units, so 2 cartons
  meet a "24 boxes" trigger of a 12-box carton. For a category scheme the trigger unit is read
  in each product's own packing (a product without that unit uses its next smaller one).

ONE scheme wins per line. Each applicable scheme is valued in rupees for that line:

* free goods: free quantity x the customer's rate for the free product in that unit;
* percentage off: what the percentage takes off the line;
* amount off: the amount per unit x the line quantity.

The scheme worth most to the customer wins. A tie goes to the more specific scheme (one
customer, then one route, then everyone; then a product scheme over a category scheme), then to
the higher trigger, then to the newest scheme.

Before valuing, a scheme is passed over when it cannot be given:

* a percentage or amount off that would take the line below the product's minimum margin;
* a percentage off when the cashier typed the line discount by hand, or an amount off when the
  rate was typed by hand (what was typed stands);
* free goods of a product that is switched off.

Free goods repeat: "10 + 1" gives 2 free on 20 and 2 free on 29. Discounts are slabs: once
the line reaches the trigger the whole line gets the discount.
"""
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from django.db.models import Q
from django.utils import timezone

from pos.models import PACK_PIECE, quantize_money, quantize_qty
from pos.services.cart_pricing import effective_unit_price, line_total, line_unit_cost

from ..models import Scheme
from .rates import RateBook, schemes_enabled

ZERO = Decimal("0")
SCOPE_RANK = {Scheme.SCOPE_CUSTOMER: 2, Scheme.SCOPE_ROUTE: 1, Scheme.SCOPE_ALL: 0}


@dataclass
class Offer:
    scheme: Scheme
    kind: str
    value: Decimal
    # free goods
    free_product: object = None
    free_quantity: Decimal = ZERO
    free_level: str = PACK_PIECE
    # discounts
    discount_percent: Decimal = ZERO
    amount_off: Decimal = ZERO  # per unit of the line, line money

    @property
    def is_free(self):
        return self.kind == Scheme.REWARD_FREE

    @property
    def name(self):
        return self.scheme.name

    def rank(self, trigger_base):
        scheme = self.scheme
        return (
            self.value,
            SCOPE_RANK.get(scheme.scope, 0),
            1 if scheme.product_id else 0,
            trigger_base,
            scheme.pk or 0,
        )


class SchemeBook:
    """The schemes running today for one customer, read once."""

    def __init__(self, customer=None, day=None, rate_book=None):
        self.customer = customer
        self.day = day or timezone.localdate()
        self.rate_book = rate_book or RateBook(customer)
        self.schemes = self._load() if schemes_enabled() else []

    def _load(self):
        day = self.day
        scope = Q(scope=Scheme.SCOPE_ALL)
        if self.customer is not None:
            scope |= Q(scope=Scheme.SCOPE_CUSTOMER, customer_id=self.customer.pk)
            if self.customer.route_id:
                scope |= Q(scope=Scheme.SCOPE_ROUTE, route_id=self.customer.route_id)
        return list(
            Scheme.objects.filter(scope, is_active=True)
            .filter(Q(start_date__isnull=True) | Q(start_date__lte=day))
            .filter(Q(end_date__isnull=True) | Q(end_date__gte=day))
            .select_related("free_product", "product", "category")
        )

    def candidates(self, product):
        return [
            scheme
            for scheme in self.schemes
            if scheme.product_id == product.pk or (scheme.product_id is None and scheme.category_id and scheme.category_id == product.category_id)
        ]

    def best_offer(self, product, level, quantity, rate, *, allow_percent=True, allow_amount=True):
        """The winning Offer for a line of ``quantity`` units of ``level`` at ``rate``, or None."""
        if not self.schemes:
            return None
        quantity = quantize_qty(quantity)
        if quantity <= 0:
            return None
        rate = quantize_money(rate)
        base_quantity = quantity * product.pack_factor(level)
        minimum = product.minimum_sale_price_for_cost(line_unit_cost(product, level))
        best, best_rank = None, None
        for scheme in self.candidates(product):
            trigger_level = product.resolve_pack_level(scheme.trigger_unit)
            trigger_base = Decimal(scheme.trigger_quantity) * product.pack_factor(trigger_level)
            if trigger_base <= 0 or base_quantity < trigger_base:
                continue
            offer = self._offer(scheme, product, level, quantity, rate, base_quantity, trigger_base, trigger_level, minimum, allow_percent, allow_amount)
            if offer is None:
                continue
            rank = offer.rank(trigger_base)
            if best_rank is None or rank > best_rank:
                best, best_rank = offer, rank
        return best

    def _offer(self, scheme, product, level, quantity, rate, base_quantity, trigger_base, trigger_level, minimum, allow_percent, allow_amount):
        if scheme.reward == Scheme.REWARD_FREE:
            free_product = scheme.free_product if scheme.free_product_id else product
            if not free_product.is_active:
                return None
            free_level = free_product.resolve_pack_level(scheme.free_unit)
            multiples = (base_quantity / trigger_base).to_integral_value(rounding=ROUND_FLOOR)
            free_quantity = quantize_qty(multiples * Decimal(scheme.free_quantity or 0))
            if free_quantity <= 0:
                return None
            value = quantize_money(free_quantity * self.rate_book.rate(free_product, free_level))
            return Offer(scheme, Scheme.REWARD_FREE, value, free_product=free_product, free_quantity=free_quantity, free_level=free_level)
        if scheme.reward == Scheme.REWARD_PERCENT:
            if not allow_percent:
                return None
            percent = quantize_money(scheme.discount_percent or 0)
            if percent <= 0 or effective_unit_price(rate, percent) < minimum:
                return None
            value = quantize_money(quantize_money(rate * quantity) - line_total(quantity, rate, percent))
            return Offer(scheme, Scheme.REWARD_PERCENT, value, discount_percent=percent)
        if scheme.reward == Scheme.REWARD_AMOUNT:
            if not allow_amount:
                return None
            # Typed per trigger unit (50 off a carton); the line may be in another unit.
            off = quantize_money(Decimal(scheme.discount_amount or 0) * product.pack_factor(level) / product.pack_factor(trigger_level))
            if off <= 0 or rate - off <= 0 or rate - off < minimum:
                return None
            return Offer(scheme, Scheme.REWARD_AMOUNT, quantize_money(off * quantity), amount_off=off)
        return None


# ------------------------------------------------------------------------- one line
@dataclass
class LinePlan:
    """What a line ends up with once its list rate and its scheme are applied."""

    original_price: Decimal
    price: Decimal
    discount_percent: Decimal
    offer: Offer = None
    scheme_price: Decimal = None  # the rate an amount-off scheme set
    scheme_discount: Decimal = None  # the percentage a scheme set

    @property
    def scheme(self):
        return self.offer.scheme if self.offer is not None else None


def plan_line(book, product, level, quantity, *, list_rate, price, discount_percent, hand_price, hand_discount):
    """Apply the winning scheme to one line. What the cashier typed by hand stays as typed."""
    offer = book.best_offer(
        product, level, quantity, list_rate, allow_percent=not hand_discount, allow_amount=not hand_price
    )
    plan = LinePlan(original_price=list_rate, price=price, discount_percent=discount_percent, offer=offer)
    if not hand_price:
        plan.price = list_rate
        if offer is not None and offer.kind == Scheme.REWARD_AMOUNT:
            plan.price = quantize_money(list_rate - offer.amount_off)
            plan.scheme_price = plan.price
    if not hand_discount:
        plan.discount_percent = ZERO
        if offer is not None and offer.kind == Scheme.REWARD_PERCENT:
            plan.discount_percent = offer.discount_percent
            plan.scheme_discount = offer.discount_percent
    return plan


def whole_units_available(product, stock_base, level):
    """How many whole units of the level the given base stock covers (None = no shelf limit)."""
    if stock_base is None:
        return None
    if stock_base <= 0:
        return ZERO
    factor = product.pack_factor(level)
    if level == PACK_PIECE and product.allows_fractional_quantity:
        return quantize_qty(stock_base)
    return (Decimal(stock_base) / factor).to_integral_value(rounding=ROUND_FLOOR)
