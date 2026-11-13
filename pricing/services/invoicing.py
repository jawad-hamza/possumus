"""Price lists and schemes when a booked order becomes an invoice.

The order keeps the rates typed when it was booked. When it is invoiced:

* each line's list rate (the ``original_price`` the invoice shows) is the customer's rate;
* a scheme is worked out on the quantity actually invoiced (after any shortage), the same
  way the till does it: an amount off applies when the order rate is the list rate, a
  percentage off when the order line has no discount of its own;
* free goods are added after the line that earned them, for whatever the shelf still holds
  once every paid line has taken its share. Free goods that cannot be supplied are reported.
"""
from decimal import Decimal

from pos.models import Product, format_qty, quantize_money, quantize_qty
from pos.services.cart_pricing import tracks_stock

from .rates import RateBook
from .schemes import SchemeBook, plan_line, whole_units_available

ZERO = Decimal("0")


def apply_order_terms(rows, *, customer, locked, stock_left, warnings, rate_book=None, day=None):
    """Set list rates and schemes on the invoice rows of an order and add the free goods.

    ``rows`` are record_sale rows (changed in place, the result has the free rows added);
    ``locked`` the locked products by id (free products are locked and added to it);
    ``stock_left`` base units each product still has after the paid lines (updated too).
    """
    rates = rate_book or RateBook(customer)
    book = SchemeBook(customer, day=day, rate_book=rates)
    rates.load([row["product_id"] for row in rows])
    planned = []
    for row in rows:
        product = locked[int(row["product_id"])]
        level = row["unit_level"]
        list_rate = rates.rate(product, level)
        price = quantize_money(row["unit_price"])
        discount = quantize_money(row["discount_percent"] or 0)
        plan = plan_line(
            book, product, level, row["quantity"],
            list_rate=list_rate, price=price, discount_percent=discount,
            hand_price=price != list_rate, hand_discount=discount != 0,
        )
        row["original_price"] = plan.original_price
        row["unit_price"] = plan.price
        row["discount_percent"] = plan.discount_percent
        offer = plan.offer
        if offer is not None:
            row["scheme_id"] = str(offer.scheme.pk)
            row["scheme_name"] = offer.name
            row["scheme_price"] = plan.scheme_price
            row["scheme_discount"] = plan.scheme_discount
        planned.append((row, offer))

    free_ids = {offer.free_product.pk for _, offer in planned if offer is not None and offer.is_free}
    missing = [pk for pk in free_ids if pk not in locked]
    if missing:
        for pk, product in Product.objects.select_for_update().select_related("category").in_bulk(missing).items():
            locked[pk] = product
            stock_left[pk] = Decimal(product.stock) if tracks_stock(product) else None

    result = []
    for row, offer in planned:
        result.append(row)
        if offer is None or not offer.is_free:
            continue
        product = locked.get(offer.free_product.pk)
        if product is None or not product.is_active:
            continue
        level = product.resolve_pack_level(offer.free_level)
        wanted = offer.free_quantity
        given = wanted
        left = stock_left.get(product.pk)
        if left is not None:
            can = whole_units_available(product, left, level)
            given = max(min(wanted, can), ZERO)
            stock_left[product.pk] = left - given * product.pack_factor(level)
        if given < wanted:
            warnings.append(
                f"Free goods short: {product.display_name}, {format_qty(quantize_qty(wanted - given))} "
                f"{product.pack_label(level)} of {offer.name} could not be given from stock."
            )
        if given <= 0:
            continue
        result.append(
            {
                "product_id": product.pk,
                "quantity": quantize_qty(given),
                "unit_level": level,
                "unit_price": ZERO,
                "original_price": ZERO,
                "discount_percent": ZERO,
                "is_free": True,
                "scheme_id": str(offer.scheme.pk),
                "scheme_name": offer.name,
            }
        )
    return result
