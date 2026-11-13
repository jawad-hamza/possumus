"""Small rules every distribution screen shares: is the module on, accounts, and lines of goods
typed with a unit (carton, box or piece)."""
from decimal import Decimal, InvalidOperation

from accounts.models import Account
from pos.models import PACK_LEVELS, PACK_PIECE, Product, normalize_pack_level, quantize_money, quantize_qty
from pos.services.cart_pricing import default_sale_level
from pos.services.field_sales import field_sales_enabled
from pos.services.modules import module_enabled

MODULE_KEY = "distribution"
ZERO = Decimal("0")


class DistributionError(ValueError):
    """A refusal with a sentence the person at the screen can act on."""


def distribution_enabled():
    """Orders, loads and recovery hang on customers, so the customers module must be on too."""
    return module_enabled(MODULE_KEY) and module_enabled("parties")


def routes_in_use():
    """Routes and order bookers show on distribution screens only while that module is on."""
    return field_sales_enabled()


def ensure_account(title, account_type):
    account, _ = Account.objects.get_or_create(title=title, defaults={"type": account_type})
    if account.type != account_type:
        raise DistributionError(f"Account '{title}' exists with the wrong type.")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def to_decimal(value, default=None):
    try:
        if value in (None, ""):
            return default
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return default


def product_choice_data(product, default_level=None):
    """What the line editor needs to know about a product, for its data attributes."""
    levels = product.pack_levels
    level = product.resolve_pack_level(default_level) if default_level else default_sale_level(product)
    return {
        "levels": [
            {
                "code": entry["code"],
                "label": entry["label"],
                "factor": str(entry["factor"]),
                "price": str(product.pack_price(entry["code"])),
                "cost": str(product.pack_cost(entry["code"])),
            }
            for entry in levels
        ],
        "default": level,
        "fractional": product.allows_fractional_quantity,
        "stock": str(product.stock) if product.track_stock else "",
        "stock_text": product.stock_display,
    }


def unit_choices(product):
    return [(entry["code"], entry["label"]) for entry in product.pack_levels]


def parse_pack_lines(data, *, rate_required=True, with_discount=False, with_condition=False):
    """Read the line arrays of an order or a stock return form.

    Returns (lines, problems, typed): ``lines`` are clean dicts for the service, ``problems``
    the sentences to show, and ``typed`` the rows as typed, for drawing the form again.
    """
    products = data.getlist("line_product")
    quantities = data.getlist("line_quantity")
    units = data.getlist("line_unit")
    rates = data.getlist("line_rate")
    discounts = data.getlist("line_discount") if with_discount else []
    conditions = data.getlist("line_condition") if with_condition else []

    def pick(values, index, default=""):
        return values[index] if index < len(values) else default

    product_ids = [value for value in products if str(value).isdigit()]
    known = Product.objects.select_related("category").in_bulk([int(value) for value in product_ids])
    lines, problems, typed = [], [], []
    for index, raw_product in enumerate(products):
        raw_quantity = pick(quantities, index).strip()
        raw_rate = pick(rates, index).strip()
        raw_unit = pick(units, index)
        raw_discount = pick(discounts, index, "0").strip() or "0"
        raw_condition = pick(conditions, index, "saleable")
        row = {
            "product_id": str(raw_product or ""),
            "quantity": raw_quantity,
            "unit": raw_unit,
            "rate": raw_rate,
            "discount": raw_discount,
            "condition": raw_condition,
            "error": "",
        }
        typed.append(row)
        if not raw_product and not raw_quantity:
            continue  # an empty line the user never filled
        product = known.get(int(raw_product)) if str(raw_product).isdigit() else None
        if product is None:
            row["error"] = "Choose a product."
            problems.append(f"Line {index + 1}: choose a product.")
            continue
        level = normalize_pack_level(raw_unit, default=PACK_PIECE)
        if level not in product.pack_level_codes:
            level = product.resolve_pack_level(level)
        quantity = to_decimal(raw_quantity)
        if quantity is None or quantity <= 0:
            row["error"] = "Enter a quantity."
            problems.append(f"Line {index + 1}: enter a quantity for {product.display_name}.")
            continue
        quantity = quantize_qty(quantity)
        quantity_error = product.validate_quantity(quantity, level)
        if quantity_error:
            row["error"] = quantity_error
            problems.append(f"Line {index + 1}: {quantity_error}")
            continue
        rate = to_decimal(raw_rate)
        if rate is None or rate < 0 or (rate_required and rate <= 0):
            row["error"] = "Enter a rate."
            problems.append(f"Line {index + 1}: enter the rate of one {product.pack_label(level)} of {product.display_name}.")
            continue
        discount = to_decimal(raw_discount, ZERO)
        if discount is None or discount < 0 or discount > 100:
            row["error"] = "Discount is a percentage from 0 to 100."
            problems.append(f"Line {index + 1}: the discount must be between 0 and 100 percent.")
            continue
        lines.append(
            {
                "product": product,
                "quantity": quantity,
                "unit_level": level,
                "unit_factor": product.pack_factor(level),
                "rate": quantize_money(rate),
                "discount_percent": quantize_money(discount),
                "condition": raw_condition,
                "row": row,
            }
        )
    return lines, problems, typed


def pack_columns(product, base_quantity):
    """{"carton": n, "box": n, "piece": n} for a base quantity, as a load sheet shows it."""
    counts = {level: ZERO for level in PACK_LEVELS}
    for level, count in product.split_quantity(base_quantity):
        counts[level] = count
    return counts


def packing_text(product):
    """How a product is packed, the way a load sheet writes it: "12x24", "24" or blank."""
    per_box = int(product.pieces_per_box or 1)
    per_carton = int(product.boxes_per_carton or 1)
    if not product.has_packaging:
        return ""
    if per_box > 1 and per_carton > 1:
        return f"{per_carton}x{per_box}"
    return str(per_box if per_box > 1 else per_carton)
