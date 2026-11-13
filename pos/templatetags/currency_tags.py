import json
from decimal import Decimal, InvalidOperation

from django import template
from django.utils.safestring import mark_safe

from pos.models import PACK_PIECE, Product, format_qty
from pos.services.currency import (
    currency_symbol as active_currency_symbol,
    format_amount,
    format_money,
    prime_currency_symbol,  # noqa: F401  (re-exported for the context processor)
)

register = template.Library()

# re-exported for the context processor
prime_currency_symbol = prime_currency_symbol


def _to_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


@register.filter
def amount(value):
    """Thousands-separated 2dp number without the currency symbol."""
    return format_amount(value)


@register.filter
def money(value):
    """Currency symbol from Settings plus a thousands-separated 2dp amount."""
    return format_money(value)


@register.filter
def qty(value):
    """Quantity without trailing zeros: 2, 0.5, 1.25."""
    if _to_decimal(value) is None:
        return value
    return format_qty(value)


@register.filter
def qty_unit(value, unit):
    """Quantity followed by its unit: "0.5 kg"."""
    unit = unit or Product.UNIT_PIECE
    return f"{qty(value)} {unit}"


@register.filter
def qty_short(value, unit):
    """Like qty_unit, but pieces stay a bare number: "2" versus "0.5 kg"."""
    if not unit or unit == Product.UNIT_PIECE:
        return qty(value)
    return qty_unit(value, unit)


@register.filter
def qty_step(product):
    """The step attribute for a quantity input of this product."""
    return getattr(product, "quantity_step", None) or "1"


@register.filter
def per_unit(product):
    """" / kg" for products not sold by the piece, empty otherwise."""
    unit = getattr(product, "unit", "") or ""
    if not unit or unit == Product.UNIT_PIECE:
        return ""
    return f" / {unit}"


# ------------------------------------------------------------------ cartons, boxes, pieces
@register.filter
def in_packs(quantity, product):
    """A base quantity in the product's packs: "12 Ctn 3 Box 5 pcs"; "12.5 kg" for a plain product."""
    if getattr(product, "has_packaging", False):
        return product.format_quantity(quantity)
    return qty_unit(quantity, getattr(product, "unit", None))


@register.filter
def in_packs_short(quantity, product):
    """Like in_packs, but a plain product counted in pieces stays a bare number."""
    if getattr(product, "has_packaging", False):
        return product.format_quantity(quantity)
    return qty_short(quantity, getattr(product, "unit", None))


@register.simple_tag
def pack_quantity(product, quantity, top_level=None):
    """A base quantity from a chosen level down: {% pack_quantity product product.stock "box" %} gives "147 Box 5 pcs"."""
    return product.format_quantity(quantity, top_level or None)


@register.filter
def price_unit(product):
    """The unit a product's shown cost and price are for: "Ctn", "Box", "kg"; empty for plain pieces."""
    if getattr(product, "has_packaging", False):
        return product.top_pack_label
    unit = getattr(product, "unit", "") or ""
    return "" if unit == Product.UNIT_PIECE else unit


@register.filter
def line_qty(item):
    """A sale, purchase or return line's quantity in the unit it was entered in: "10 Ctn", "5 pcs".

    A plain product keeps the short form it always had: "2", "0.5 kg".
    """
    if item.unit_level == PACK_PIECE and not item.product.has_packaging:
        return qty_short(item.quantity, item.product.unit)
    return item.quantity_display


@register.filter
def line_unit(item):
    """The unit a line's rate is for: "Ctn", "Box", "kg"; empty for plain pieces."""
    if item.unit_level != PACK_PIECE or item.product.has_packaging:
        return item.unit_label
    unit = item.product.unit or ""
    return "" if unit == Product.UNIT_PIECE else unit


@register.simple_tag
def currency_symbol():
    return active_currency_symbol()


# Quick-pick quantities offered on the POS cart, in the product's own unit.
QUANTITY_CHIPS = {
    "kg": (("250 g", "0.25"), ("500 g", "0.5"), ("1 kg", "1"), ("2 kg", "2")),
    "g": (("100 g", "100"), ("250 g", "250"), ("500 g", "500")),
    "l": (("250 ml", "0.25"), ("500 ml", "0.5"), ("1 L", "1")),
    "ml": (("100 ml", "100"), ("250 ml", "250"), ("500 ml", "500")),
    "m": (("0.5 m", "0.5"), ("1 m", "1"), ("2 m", "2")),
    "ft": (("0.5 ft", "0.5"), ("1 ft", "1"), ("2 ft", "2")),
}


@register.simple_tag
def quantity_chips(unit, stock=None):
    """Quick quantities for a fractional unit; a chip above the stock on hand is disabled."""
    available = _to_decimal(stock) if stock is not None else None
    return [
        {
            "label": label,
            "quantity": quantity,
            "disabled": available is not None and Decimal(quantity) > available,
        }
        for label, quantity in QUANTITY_CHIPS.get(unit, ())
    ]


@register.simple_tag
def fractional_units_json():
    """JSON list of units sold in fractions, for inline scripts that set quantity steps."""
    return mark_safe(json.dumps(sorted(Product.FRACTIONAL_UNITS)))


@register.simple_tag
def piece_unit():
    return Product.UNIT_PIECE
