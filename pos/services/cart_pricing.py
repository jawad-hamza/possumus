from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from pos.models import PACK_LEVELS, PACK_PIECE, SystemSettings, format_qty, quantize_money, quantize_qty
from pos.services.currency import currency_symbol


ZERO = Decimal("0")
HUNDRED = Decimal("100")


def default_sale_level(product):
    """The unit a product goes on the bill in: the till's default, or the next smaller one it has."""
    if not product.has_packaging:
        return PACK_PIECE
    return product.resolve_pack_level(SystemSettings.load().till_default_unit)


def sale_level_in_stock(product, level):
    """The given level, or the largest smaller one the shelf can still cover one of."""
    if not tracks_stock(product):
        return level
    available = product.pack_level_codes
    for candidate in PACK_LEVELS[PACK_LEVELS.index(level):]:
        if candidate in available and product.stock >= product.pack_factor(candidate):
            return candidate
    return PACK_PIECE


def cart_level(product, item):
    """The unit a cart line is in. A level the product no longer has falls to the next smaller one."""
    return product.resolve_pack_level((item or {}).get("unit") or PACK_PIECE)


def seed_cart_item(product, level=None):
    level = product.resolve_pack_level(level) if level else default_sale_level(product)
    price = product.pack_price(level)
    return {
        "name": product.display_name,
        "price": str(price),
        "original_price": str(price),
        "discount_percent": "0.00",
        "quantity": "1",
        "unit": level,
    }



def to_decimal(value, default=ZERO):
    try:
        if value in (None, ""):
            return default
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return default



def clamp_discount_percent(value):
    discount = to_decimal(value)
    if discount < ZERO:
        discount = ZERO
    if discount > HUNDRED:
        discount = HUNDRED
    return quantize_money(discount)



def effective_unit_price(unit_price, discount_percent):
    unit = quantize_money(to_decimal(unit_price))
    discount = clamp_discount_percent(discount_percent)
    multiplier = Decimal("1") - (discount / HUNDRED)
    return quantize_money(unit * multiplier)



def line_total(quantity, unit_price, discount_percent):
    return quantize_money(effective_unit_price(unit_price, discount_percent) * to_decimal(quantity))



def line_unit_cost(product, level=PACK_PIECE):
    """What one unit of the line's level costs: the figure the minimum margin is measured against."""
    factor = product.pack_factor(level)
    if factor == 1:
        return product.cost_price
    return Decimal(product.cost_price or 0) * factor


def validate_cart_line(product, unit_price, discount_percent, level=PACK_PIECE):
    unit = quantize_money(to_decimal(unit_price))
    discount = clamp_discount_percent(discount_percent)

    if unit <= ZERO:
        raise ValidationError("Custom price must be greater than zero.")

    minimum_price = product.minimum_sale_price_for_cost(line_unit_cost(product, level))
    final_unit_price = effective_unit_price(unit, discount)
    if final_unit_price < minimum_price:
        raise ValidationError(
            f"Final unit price cannot go below {currency_symbol()} {minimum_price:.2f}."
        )

    return {
        "price": unit,
        "discount_percent": discount,
        "final_unit_price": final_unit_price,
        "minimum_price": minimum_price,
    }



def tracks_stock(product):
    """Services and other non-stock items skip shelf checks once Product.track_stock exists."""
    return bool(getattr(product, "track_stock", True))


def stock_shortfall(product, quantity, level=PACK_PIECE):
    """Readable reason the shelf cannot cover this quantity (in the level's unit), or '' when it can."""
    if not tracks_stock(product):
        return ""
    if product.stock <= ZERO:
        return "Out of stock."
    if to_decimal(quantity) * product.pack_factor(level) > product.stock:
        return f"Only {product.stock_display} in stock."
    return ""


def cart_quantity(value):
    """Cart quantities live in the session as strings so weights like 0.5 survive JSON."""
    quantity = quantize_qty(to_decimal(value))
    return quantity if quantity > ZERO else ZERO


def normalize_cart_item(item):
    quantity = format_qty(cart_quantity(item.get("quantity", 0)))
    price = quantize_money(to_decimal(item.get("price")))
    original_price = quantize_money(to_decimal(item.get("original_price"), price))
    discount_percent = clamp_discount_percent(item.get("discount_percent", 0))

    return {
        **item,
        "quantity": quantity,
        "price": str(price),
        "original_price": str(original_price),
        "discount_percent": str(discount_percent),
    }
