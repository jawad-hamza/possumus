"""Price lists and schemes on the till's bill.

The bill lives in the session as ``{product id: line}``. Two things are added here:

* ``apply_terms`` runs after every change to the bill (and before checkout). It re-prices each
  line for the sale's customer (customer's list, then route's list, then product price) when
  the lists in force change, and works out the line's scheme. A rate or discount the cashier
  typed by hand is never overwritten.
* ``with_free_goods`` turns the free goods a line earned into their own bill rows, priced at
  zero and tagged with the scheme. They are not stored in the session: they follow the paid
  line, so changing its quantity changes them. The shelf is checked across the whole bill and
  free goods get only what the paid lines leave; anything short is noted on the free line.
"""
from decimal import Decimal

from pos.models import Customer, Product, SystemSettings, format_qty, quantize_money, quantize_qty
from pos.services.cart_pricing import cart_level, cart_quantity, clamp_discount_percent, to_decimal, tracks_stock

from .rates import RateBook, price_lists_enabled, schemes_enabled
from .schemes import SchemeBook, plan_line, whole_units_available

FREE_PREFIX = "free-"
ZERO = Decimal("0")
SCHEME_KEYS = ("scheme_id", "scheme_name", "scheme_kind", "scheme_price", "scheme_discount", "free", "free_declined")


def is_free_line_id(value):
    return str(value or "").startswith(FREE_PREFIX)


def free_line_id(product_id):
    return f"{FREE_PREFIX}{product_id}"


def paid_line_of(line_id):
    return str(line_id or "")[len(FREE_PREFIX):]


def _decimal_or_none(value):
    if value in (None, ""):
        return None
    return to_decimal(value, None)


def sale_customer(customer_id):
    customer_id = str(customer_id or "").strip()
    if not customer_id.isdigit():
        return None
    return (
        Customer.objects.filter(pk=customer_id, is_active=True)
        .select_related("price_list", "route", "route__price_list")
        .first()
    )


def _clear_schemes(item):
    """Take a scheme off a line: a rate or discount it set goes back to the list rate / nothing."""
    scheme_price = _decimal_or_none(item.get("scheme_price"))
    if scheme_price is not None and to_decimal(item.get("price")) == scheme_price:
        item["price"] = item.get("original_price") or item.get("price")
    scheme_discount = _decimal_or_none(item.get("scheme_discount"))
    if scheme_discount is not None and to_decimal(item.get("discount_percent")) == scheme_discount:
        item["discount_percent"] = "0.00"
    for key in SCHEME_KEYS:
        item.pop(key, None)


def apply_terms(cart, customer_id=None, *, customer=None):
    """Re-price the bill for its customer and work out each line's scheme. Changes ``cart`` in place."""
    if not cart:
        return cart
    lists_on, schemes_on = price_lists_enabled(), schemes_enabled()
    if not lists_on and not schemes_on:
        for item in cart.values():
            if any(key in item for key in SCHEME_KEYS) or item.get("rate_key"):
                _clear_schemes(item)
                if item.pop("rate_key", None):
                    # The line was priced from a list that no longer applies: back to the product price.
                    item["_reprice"] = True
        if not any(item.get("_reprice") for item in cart.values()):
            return cart
    if customer is None:
        customer = sale_customer(customer_id)
    rates = RateBook(customer)
    book = SchemeBook(customer, rate_book=rates)
    ids = [int(pk) for pk in cart.keys() if str(pk).isdigit()]
    products = Product.objects.select_related("category").in_bulk(ids)
    rates.load(ids)
    key = rates.key
    for product_id, item in cart.items():
        product = products.get(int(product_id)) if str(product_id).isdigit() else None
        if product is None or not product.is_active:
            continue
        level = cart_level(product, item)
        quantity = cart_quantity(item.get("quantity"))
        price = quantize_money(to_decimal(item.get("price")))
        original = quantize_money(to_decimal(item.get("original_price"), price))
        discount = clamp_discount_percent(item.get("discount_percent"))
        scheme_price = _decimal_or_none(item.get("scheme_price"))
        scheme_discount = _decimal_or_none(item.get("scheme_discount"))
        hand_price = price != original and not (scheme_price is not None and price == scheme_price)
        hand_discount = discount != 0 and not (scheme_discount is not None and discount == scheme_discount)
        stored_key = item.get("rate_key") or "0"
        reprice = item.pop("_reprice", False) or stored_key != key
        list_rate = rates.rate(product, level) if reprice else original

        plan = plan_line(
            book, product, level, quantity,
            list_rate=list_rate, price=price, discount_percent=discount,
            hand_price=hand_price, hand_discount=hand_discount,
        )
        item["original_price"] = str(quantize_money(plan.original_price))
        item["price"] = str(quantize_money(plan.price))
        item["discount_percent"] = str(quantize_money(plan.discount_percent))
        if key != "0":
            item["rate_key"] = key
        else:
            item.pop("rate_key", None)

        declined = item.get("free_declined")
        for name in SCHEME_KEYS:
            item.pop(name, None)
        offer = plan.offer
        if offer is None:
            continue
        item["scheme_id"] = str(offer.scheme.pk)
        item["scheme_name"] = offer.name
        item["scheme_kind"] = offer.kind
        if plan.scheme_price is not None:
            item["scheme_price"] = str(plan.scheme_price)
        if plan.scheme_discount is not None:
            item["scheme_discount"] = str(plan.scheme_discount)
        if offer.is_free:
            item["free"] = {
                "product_id": str(offer.free_product.pk),
                "quantity": format_qty(offer.free_quantity),
                "unit": offer.free_level,
            }
            # A free line the cashier took off stays off while the same scheme gives it.
            if declined and declined == item["scheme_id"]:
                item["free_declined"] = declined
    return cart


def line_scheme_fields(item, unit_price, original_price):
    """What a paid bill row shows about its scheme."""
    scheme_price = _decimal_or_none(item.get("scheme_price"))
    scheme_discount = _decimal_or_none(item.get("scheme_discount"))
    by_scheme_price = scheme_price is not None and unit_price == scheme_price
    by_scheme_discount = scheme_discount is not None and to_decimal(item.get("discount_percent")) == scheme_discount
    benefit = item.get("scheme_kind") in ("percent", "amount") and (by_scheme_price or by_scheme_discount)
    return {
        "line_id": "",
        "is_free": False,
        "scheme_id": item.get("scheme_id", "") if benefit else "",
        "scheme_name": item.get("scheme_name", "") if benefit else "",
        "scheme_price": scheme_price if by_scheme_price else None,
        "scheme_discount": scheme_discount if by_scheme_discount else None,
        "scheme_amount_off": quantize_money(original_price - unit_price) if by_scheme_price else ZERO,
        "earns_free": bool(item.get("free")),
    }


def free_product_ids(cart):
    return sorted(
        {
            int(item["free"]["product_id"])
            for item in (cart or {}).values()
            if isinstance(item.get("free"), dict) and str(item["free"].get("product_id", "")).isdigit()
        }
    )


def _free_row(paid_row, item, product, level, wanted, given, removable):
    factor = product.pack_factor(level)
    short = quantize_qty(wanted - given)
    note = ""
    if short > 0:
        note = (
            f"Only {format_qty(given)} {product.pack_label(level)} left on the shelf for the free goods; "
            f"{format_qty(short)} {product.pack_label(level)} short."
        )
    return {
        "product_id": str(product.pk),
        "line_id": free_line_id(paid_row["product_id"]),
        "paid_line": str(paid_row["product_id"]),
        "is_free": True,
        "removable": removable,
        "product": product,
        "name": product.display_name,
        "quantity": given,
        "quantity_display": format_qty(given),
        "wanted_quantity": wanted,
        "unit": product.pack_label(level),
        "unit_level": level,
        "unit_factor": factor,
        "base_quantity": product.to_base_quantity(given, level),
        "unit_options": [],
        "per_unit": "",
        "max_quantity": None,
        "allows_fraction": False,
        "quantity_step": "1",
        "unit_price": ZERO,
        "original_price": ZERO,
        "discount_percent": ZERO,
        "effective_unit_price": ZERO,
        "line_total": ZERO,
        "line_discount_amount": ZERO,
        "minimum_price": ZERO,
        "category_tax_rate": ZERO,
        "cash_category_tax_rate": ZERO,
        "bank_category_tax_rate": ZERO,
        "stock": product.stock if tracks_stock(product) else None,
        "has_custom_price": False,
        "has_line_discount": False,
        "pricing_error": "",
        "stock_error": "",
        "free_note": note,
        "scheme_id": item.get("scheme_id", ""),
        "scheme_name": item.get("scheme_name", ""),
        "scheme_price": None,
        "scheme_discount": None,
        "scheme_amount_off": ZERO,
        "earns_free": False,
    }


def with_free_goods(cart, rows):
    """The bill rows with a free-goods row after each line that earned one."""
    specs = {
        str(product_id): item
        for product_id, item in (cart or {}).items()
        if isinstance(item.get("free"), dict) and item.get("free_declined") != item.get("scheme_id")
    }
    if not specs or not schemes_enabled():
        return rows
    products = Product.objects.in_bulk(free_product_ids({key: item for key, item in specs.items()}))
    used = {}
    for row in rows:
        product = row.get("product")
        if product is not None and not row.get("is_free"):
            used[product.pk] = used.get(product.pk, ZERO) + Decimal(row.get("base_quantity") or 0)
    removable = bool(SystemSettings.load().allow_remove_free_goods)
    result = []
    for row in rows:
        result.append(row)
        item = specs.get(str(row["product_id"]))
        if item is None or row.get("product") is None:
            continue
        spec = item["free"]
        product = products.get(int(spec["product_id"]))
        if product is None or not product.is_active:
            continue
        level = product.resolve_pack_level(spec.get("unit"))
        wanted = cart_quantity(spec.get("quantity"))
        given = wanted
        if tracks_stock(product):
            left = Decimal(product.stock) - used.get(product.pk, ZERO)
            can = whole_units_available(product, left, level)
            given = max(min(wanted, can), ZERO)
        used[product.pk] = used.get(product.pk, ZERO) + given * product.pack_factor(level)
        result.append(_free_row(row, item, product, level, wanted, given, removable))
    return result


def decline_free_goods(cart, line_id):
    """Take a free-goods line off the bill. Returns the reason it was refused, or ''."""
    paid = paid_line_of(line_id)
    item = cart.get(paid)
    if item is None or not item.get("free"):
        return ""
    if not SystemSettings.load().allow_remove_free_goods:
        return "Free goods come with the scheme and stay on the bill. The owner can allow taking them off in Settings."
    item["free_declined"] = item.get("scheme_id", "")
    return ""


def free_line_refusal():
    return "Free goods follow the scheme. Change the quantity on the line that earned them."
