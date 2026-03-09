from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from pos.models import quantize_money


ZERO = Decimal("0")
HUNDRED = Decimal("100")


def seed_cart_item(product):
    return {
        "name": product.name,
        "price": str(quantize_money(product.price)),
        "original_price": str(quantize_money(product.price)),
        "discount_percent": "0.00",
        "quantity": 1,
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



def validate_cart_line(product, unit_price, discount_percent):
    unit = quantize_money(to_decimal(unit_price))
    discount = clamp_discount_percent(discount_percent)

    if unit <= ZERO:
        raise ValidationError("Custom price must be greater than zero.")

    minimum_price = product.minimum_sale_price_for_cost(product.cost_price)
    final_unit_price = effective_unit_price(unit, discount)
    if final_unit_price < minimum_price:
        raise ValidationError(
            f"Final unit price cannot go below Rs {minimum_price:.2f}."
        )

    return {
        "price": unit,
        "discount_percent": discount,
        "final_unit_price": final_unit_price,
        "minimum_price": minimum_price,
    }



def normalize_cart_item(item):
    quantity = int(item.get("quantity", 0) or 0)
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
