"""The bill discount: typed as a percentage or as a cash amount, shown as both.

One figure drives every calculation: ``Sale.discount_rate``, a percentage kept to ten
decimals. A discount typed as a percentage is that rate, to two decimals as typed. A
discount typed as cash is turned into the rate that gives the same amount back on the
bill's subtotal, so the invoice, sale returns, the reports and the SQL that multiplies
by ``1 - rate%`` all agree with what the cashier typed, to the paisa.

Why ten decimals are enough: rounding the rate to ten places moves ``subtotal x rate%``
by at most ``subtotal x 0.0000000000005``. That stays under half a paisa for every
subtotal below ten thousand million, which is more than the money columns can hold.

``Sale.discount_type`` and ``Sale.discount_value`` record how the discount was typed.
They decide the wording ("Discount 10% = Rs 20.00" or "Discount Rs 20.00 (9.3%)") and,
while the bill is still open, which figure stays put when lines are added or removed.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from pos.models import quantize_money
from pos.services.currency import format_amount, format_money

PERCENT = "percent"
AMOUNT = "amount"

ZERO = Decimal("0")
HUNDRED = Decimal("100")
RATE_PLACES = Decimal("0.0000000001")
# The largest figure a 12-digit money column holds.
MAX_AMOUNT = Decimal("9999999999.99")
DISCOUNT_KEYS = ("discount_type", "discount_value", "discount_rate", "preserve_discount_rate")


def to_decimal(value):
    """A typed figure as a Decimal; nothing, nonsense and infinities read as zero."""
    if value in (None, ""):
        return ZERO
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return ZERO
    return number if number.is_finite() else ZERO


def clean_discount_type(value):
    return AMOUNT if str(value or "").strip().lower() == AMOUNT else PERCENT


def typed_percent(value):
    """A percentage as the cashier types it: 0 to 100, two decimals."""
    rate = to_decimal(value)
    if rate < 0:
        return ZERO
    if rate > 100:
        return HUNDRED
    return quantize_money(rate)


def normalized_discount_rate(value):
    """The rate calculations use: 0 to 100, kept to ten decimals so a cash discount survives the round trip."""
    rate = to_decimal(value)
    if rate < 0:
        return ZERO
    if rate > 100:
        return HUNDRED
    return rate.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def clean_cash_discount(value):
    """A cash amount as typed, to the paisa. A negative stays negative so it can be refused by name."""
    amount = to_decimal(value)
    amount = min(max(amount, -MAX_AMOUNT), MAX_AMOUNT)
    return quantize_money(amount)


def rate_for_amount(amount, subtotal):
    """The percentage of ``subtotal`` that ``amount`` is, to ten decimals."""
    amount = to_decimal(amount)
    subtotal = to_decimal(subtotal)
    if amount <= 0 or subtotal <= 0:
        return ZERO
    if amount >= subtotal:
        return HUNDRED
    return (amount * HUNDRED / subtotal).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def amount_for_rate(subtotal, rate):
    """What a rate takes off a subtotal, to the paisa."""
    return quantize_money(to_decimal(subtotal) * (normalized_discount_rate(rate) / HUNDRED))


def cash_discount_on(value, subtotal):
    """(amount, rate) of a cash discount on these lines. It never takes off more than the lines come to."""
    subtotal = to_decimal(subtotal)
    amount = min(max(clean_cash_discount(value), ZERO), max(quantize_money(subtotal), ZERO))
    return amount, rate_for_amount(amount, subtotal)


def format_percent(value):
    """Up to two decimals without trailing zeros: 10, 12.5, 9.3."""
    number = to_decimal(value)
    rounded = quantize_money(number)
    if number > 0 and rounded == 0:
        return "under 0.01"
    text = f"{rounded:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def discount_equals(percent, amount, money=format_money):
    """A percentage with what it comes to: "10% = Rs 20.00"."""
    return f"{format_percent(percent)}% = {money(amount)}"


@dataclass(frozen=True)
class BillDiscount:
    """A bill's discount worked out on a subtotal."""

    type: str
    value: Decimal  # the figure as typed: a percentage, or a cash amount
    rate: Decimal  # the percentage every calculation uses
    amount: Decimal  # what it takes off this subtotal
    subtotal: Decimal
    error: str = ""

    @property
    def is_amount(self):
        return self.type == AMOUNT

    @property
    def percent(self):
        """The rate to two decimals, for a box that shows a percentage."""
        return quantize_money(self.rate)

    @property
    def percent_text(self):
        return f"{format_percent(self.rate)}%"

    def parts(self, money=format_money):
        """The wording in two halves, for a row with its figure on the right."""
        if self.is_amount:
            return "Discount", f"{money(self.amount)} ({self.percent_text})"
        return f"Discount {self.percent_text} =", money(self.amount)

    def label(self, money=format_money):
        """"Discount 10% = Rs 20.00" when typed as a percentage, "Discount Rs 20.00 (9.3%)" when typed as cash."""
        return " ".join(self.parts(money))

    def cell(self):
        """The same wording for a report column headed "Discount": no word, no currency symbol."""
        if self.amount <= 0:
            return format_amount(self.amount)
        if self.is_amount:
            return f"{format_amount(self.amount)} ({self.percent_text})"
        return discount_equals(self.rate, self.amount, format_amount)

    def other(self, money=format_money):
        """The figure that was not typed, worded to read on from the box: "= Rs 20.00" beside 10 %, "(9.3%)" beside Rs 20."""
        if self.error:
            return ""
        return f"({self.percent_text})" if self.is_amount else f"= {money(self.amount)}"

    # Template-friendly names (templates cannot pass the money formatter).
    @property
    def left(self):
        return self.parts()[0]

    @property
    def right(self):
        return self.parts()[1]

    @property
    def other_display(self):
        return self.other()


def resolve_bill_discount(discount_type, value, subtotal, *, has_lines=True):
    """Work the typed discount out on a subtotal.

    A percentage always applies. A cash amount stays what was typed; it is refused, with
    the reason in ``error``, when it is negative or more than the lines come to. On an
    empty bill a cash amount simply waits for the first line.
    """
    subtotal = quantize_money(to_decimal(subtotal))
    if clean_discount_type(discount_type) == AMOUNT:
        amount = clean_cash_discount(value)
        error = ""
        if amount < 0:
            error = "A discount cannot be less than zero."
        elif has_lines and amount > subtotal:
            error = f"Discount {format_money(amount)} is more than the bill's subtotal of {format_money(subtotal)}."
        if error or not has_lines:
            return BillDiscount(AMOUNT, amount, ZERO, ZERO, subtotal, error)
        return BillDiscount(AMOUNT, amount, rate_for_amount(amount, subtotal), amount, subtotal)

    rate = typed_percent(value)
    return BillDiscount(PERCENT, rate, rate, amount_for_rate(subtotal, rate), subtotal)


def sale_bill_discount(sale, subtotal=None):
    """The discount of a saved sale, for the invoice, the receipt and the reports."""
    if subtotal is None:
        subtotal = sale.subtotal
    subtotal = quantize_money(to_decimal(subtotal))
    rate = normalized_discount_rate(getattr(sale, "discount_rate", 0))
    amount = amount_for_rate(subtotal, rate)
    if clean_discount_type(getattr(sale, "discount_type", PERCENT)) == AMOUNT:
        return BillDiscount(AMOUNT, amount, rate, amount, subtotal)
    return BillDiscount(PERCENT, rate, rate, amount, subtotal)


# ---------------------------------------------------------------------------
# The till: reading what was typed, keeping it in the session
# ---------------------------------------------------------------------------

def typed_discount(*sources):
    """(type, typed figure) from the first source that carries a discount.

    Sources are request data or a stored till context, most trusted first. A source that
    posts only ``discount_rate`` means a percentage, as it always has.
    """
    for source in sources:
        if not source or not any(key in source for key in DISCOUNT_KEYS):
            continue
        kind = clean_discount_type(source.get("discount_type"))
        if kind == AMOUNT:
            raw = source.get("discount_value", source.get("discount_rate"))
        else:
            raw = source.get(
                "preserve_discount_rate", source.get("discount_rate", source.get("discount_value"))
            )
        return kind, str(raw if raw not in (None, "") else "0")
    return PERCENT, "0"


def clean_discount_context(values):
    """The discount keys of the till context. The rate of a cash discount is worked out when the bill is known."""
    if clean_discount_type(values.get("discount_type")) == AMOUNT:
        return {
            "discount_type": AMOUNT,
            "discount_value": str(clean_cash_discount(values.get("discount_value"))),
            "discount_rate": "0",
        }
    rate = str(typed_percent(values.get("discount_rate")))
    return {"discount_type": PERCENT, "discount_value": rate, "discount_rate": rate}


def till_discount_payload(discount):
    """What the till page needs to show the discount box, its unit and the other figure."""
    return {
        "type": discount.type,
        "value": str(quantize_money(discount.value)),
        "rate": str(discount.rate),
        "percent": str(discount.percent),
        "amount": str(discount.amount),
        "other": discount.other(),
        "label": "" if discount.error else discount.label(),
        "error": discount.error,
    }


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def apply_typed_discount(sale, typed, rate):
    """Record on a new sale how its discount was typed. ``typed`` is (type, figure) or None for a plain rate."""
    kind, value = typed or (PERCENT, rate)
    if clean_discount_type(kind) == AMOUNT:
        sale.discount_type = AMOUNT
        sale.discount_value = clean_cash_discount(value)
        # Sale.update_total works the rate out once the lines are in.
        sale.discount_rate = ZERO
    else:
        sale.discount_type = PERCENT
        sale.discount_value = typed_percent(rate)
    return sale


def sale_discount_refusal(sale):
    """Why a sale's cash discount cannot stand on the lines it ended up with, or an empty string."""
    if clean_discount_type(getattr(sale, "discount_type", PERCENT)) != AMOUNT:
        return ""
    return resolve_bill_discount(AMOUNT, sale.discount_value, sale.subtotal).error


# ---------------------------------------------------------------------------
# Sale returns
# ---------------------------------------------------------------------------

def line_shares(sale, items=None):
    """What each line of a sale came to after its share of the bill discount, keyed by line id.

    The discount is shared out in proportion to the line totals, each share to the paisa and the
    last line taking what is left, as category tax does, so the shares add up to exactly the
    subtotal less the discount.
    """
    items = sorted(sale.items.all() if items is None else items, key=lambda item: item.pk)
    subtotal = sum((item.line_total for item in items), ZERO)
    discount = amount_for_rate(subtotal, sale.discount_rate)
    shares = {}
    left = discount
    for index, item in enumerate(items):
        if index == len(items) - 1:
            given = left
        else:
            # the same sum Sale._category_tax_amount does, so the tax given back matches the tax charged
            given = quantize_money(discount * (item.line_total / subtotal)) if subtotal > 0 else ZERO
            left -= given
        shares[item.pk] = item.line_total - given
    return shares


def returned_net(sale, item, line_amount, completes_line, default):
    """What a return of ``line_amount`` of this line gives back before tax.

    A sale whose discount was typed as cash gives back each line's share of what was paid, and
    the last of a line gives back exactly what is left of that share, so returning everything
    refunds the sale's net to the paisa. A percentage sale keeps ``default``, as it always did.
    """
    if clean_discount_type(getattr(sale, "discount_type", PERCENT)) != AMOUNT:
        return default
    share = line_shares(sale).get(item.pk)
    line_total = item.line_total
    if share is None or line_total <= 0:
        return default
    if completes_line:
        already = sum((Decimal(row.net_amount or 0) for row in item.return_items.all()), ZERO)
        return max(quantize_money(share - already), ZERO)
    return quantize_money(share * Decimal(line_amount) / line_total)


# ---------------------------------------------------------------------------
# The printed receipt
# ---------------------------------------------------------------------------

def receipt_item_notes(item, money=format_money):
    """The small notes under a receipt line, one per printed line."""
    notes = []
    if getattr(item, "is_free", False):
        return [f"Free: {item.scheme_name}" if item.scheme_name else "Free"]
    if getattr(item, "scheme_name", ""):
        notes.append(f"Scheme: {item.scheme_name}")
    elif item.price != item.original_price:
        notes.append(f"Custom {money(item.price)}")
    if item.discount_percent > 0:
        notes.append(f"Disc {discount_equals(item.discount_percent, item.line_discount_amount, money)}")
    return notes
