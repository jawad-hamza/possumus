"""POS screen, cart helpers, barcode/category ajax, invoices and sale returns."""
import logging
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from reportlab.lib.pagesizes import mm
from reportlab.pdfgen import canvas

from accounts.services.bank_receipts import attach_optional_receipt, attach_receipt, request_upload
from accounts.services.cash_bank import bank_account_queryset
from accounts.services.party_badges import get_customer_badge
from accounts.services.pos_posting import post_sale_from_pos
from ..forms import CustomerForm, SaleReturnForm
from ..models import (
    PACK_PIECE,
    Category,
    Customer,
    Product,
    Sale,
    SaleReturn,
    format_qty,
    normalize_pack_level,
    quantize_money,
)
from ..services.access import (
    ACCESS_MANAGE,
    ACCESS_VIEW,
    any_module_access_required,
    feature_required,
    module_access_required,
    module_disabled_response,
    pos_access_required,
    user_can_access_module,
    user_can_access_sale,
)
from ..services.modules import module_enabled
from ..services.cart_pricing import (
    cart_level,
    cart_quantity,
    default_sale_level,
    effective_unit_price,
    line_total as cart_line_total,
    line_unit_cost,
    normalize_cart_item,
    sale_level_in_stock,
    seed_cart_item,
    stock_shortfall,
    to_decimal,
    tracks_stock,
    validate_cart_line,
)
from ..services import discounts as bill_discounts
from ..services.held_carts import hold_cart, list_held_carts, pop_held_cart
from ..services.field_sales import sale_order_booker, till_customer_terms
from ..services.navigation import (
    build_customer_add_url,
    get_safe_next_url,
    pop_customer_for_pos,
    remember_customer_for_pos,
)
from ..services.returns import create_sale_return, return_row
from ..services.sale_payment_change import PaymentChangeRefused, can_change_to_credit, change_sale_to_credit
from ..services.sale_recording import SaleRefused, record_sale
from pricing.services.till import (
    apply_terms,
    decline_free_goods,
    free_line_refusal,
    free_product_ids,
    is_free_line_id,
    line_scheme_fields,
    with_free_goods,
)
from ..services.business_rules import (
    category_tax_rate_for_product,
    default_brand_name,
    get_active_company_profile,
    get_system_settings,
    payment_method_totals_for_sale,
    sale_tax_amount,
    sale_tax_label,
    sale_totals_from_rows,
)
from ..services.currency import format_amount, format_money
from ._shared import _normalized_rate, _party_opening_form, _save_party_opening, safe_decimal


logger = logging.getLogger(__name__)


# Raised inside the checkout savepoint so the half-built sale rolls back with a cashier-readable reason.
CheckoutRefused = SaleRefused


def _build_pos_url(
    *,
    category=None,
    customer_id=None,
    credit=False,
    payment_method=None,
    bank_account_id=None,
    discount_rate=None,
    discount_type=None,
    discount_value=None,
    tax_rate=None,
):
    params = {}
    if category:
        params["category"] = category
    if customer_id:
        params["customer_id"] = customer_id
    if credit:
        params["credit"] = "1"
    if payment_method and payment_method != Sale.CASH:
        params["payment_method"] = payment_method
    if bank_account_id and payment_method == Sale.BANK:
        params["bank_account_id"] = bank_account_id
    if discount_type == bill_discounts.AMOUNT:
        # A discount typed as cash travels as its amount; its rate is worked out on the bill.
        discount_rate = None
        if safe_decimal(discount_value) > 0:
            params["discount_type"] = discount_type
            params["discount_value"] = str(bill_discounts.clean_cash_discount(discount_value))
    if discount_rate and safe_decimal(discount_rate) > 0:
        params["discount_rate"] = str(_normalized_rate(discount_rate))
    if tax_rate and safe_decimal(tax_rate) > 0:
        params["tax_rate"] = str(_normalized_rate(tax_rate))

    base_url = reverse("pos_screen")
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


def _normalize_cart(cart):
    normalized = {}
    for product_id, item in (cart or {}).items():
        fixed = normalize_cart_item(item)
        if cart_quantity(fixed["quantity"]) > 0:
            normalized[str(product_id)] = fixed
    return normalized


def _save_cart(request, cart):
    request.session["cart"] = _normalize_cart(cart)
    request.session.modified = True


def _add_product_to_cart(cart, product):
    product_id = str(product.id)

    if product_id in cart:
        line = normalize_cart_item(cart[product_id])
        new_quantity = cart_quantity(line["quantity"]) + Decimal("1")
        if stock_shortfall(product, new_quantity, cart_level(product, line)):
            return False
        line["quantity"] = format_qty(new_quantity)
        cart[product_id] = line
        return True

    if not tracks_stock(product):
        cart[product_id] = seed_cart_item(product)
        return True

    if product.stock < Decimal("1") and not (product.allows_fractional_quantity and product.stock > 0):
        return False
    if product.stock < Decimal("1"):
        # Less than one unit left of a weighed item: offer what remains.
        line = seed_cart_item(product, PACK_PIECE)
        line["quantity"] = format_qty(product.stock)
        cart[product_id] = line
        return True

    # A product goes on the bill in the till's default unit (the box); when less than one of
    # those is left it goes on in the largest smaller unit the shelf can still cover.
    cart[product_id] = seed_cart_item(product, sale_level_in_stock(product, default_sale_level(product)))
    return True


def _find_product_by_code(code):
    """The active product a scanned or typed code names (SKU or barcode), locked for the cart change."""
    return (
        Product.objects.select_for_update()
        .filter(Q(sku__iexact=code) | Q(barcode__iexact=code), is_active=True)
        .first()
    )


def _decrease_cart_line(cart, product_id):
    """One unit less; the line goes when nothing is left."""
    if product_id not in cart:
        return
    remaining = cart_quantity(cart[product_id]["quantity"]) - Decimal("1")
    if remaining <= 0:
        cart.pop(product_id, None)
    else:
        cart[product_id]["quantity"] = format_qty(remaining)


def _set_line_quantity(cart, product, requested):
    """Write a typed quantity onto the line. Returns the reason it was refused, or ''."""
    product_id = str(product.id)
    if product_id not in cart:
        cart[product_id] = seed_cart_item(product)
    if requested <= 0:
        cart.pop(product_id, None)
        return ""
    level = cart_level(product, cart[product_id])
    quantity_error = product.validate_quantity(requested, level)
    if quantity_error:
        return quantity_error
    shortfall = stock_shortfall(product, requested, level)
    if shortfall:
        return f"{product.display_name}: {shortfall}"
    cart[product_id]["quantity"] = format_qty(requested)
    return ""


def _set_line_unit(cart, product, requested_level, quantity=None):
    """Switch a bill line between carton, box and piece. Returns the reason it was refused, or ''.

    The number on the line stays as typed (2 boxes become 2 cartons) unless a new ``quantity``
    comes with the change; the rate follows the unit. A rate the cashier typed by hand is
    carried over in proportion.
    """
    product_id = str(product.id)
    if product_id not in cart:
        return "Cart line no longer exists."
    line = normalize_cart_item(cart[product_id])
    old_level = cart_level(product, line)
    new_level = normalize_pack_level(requested_level, default="")
    if new_level not in product.pack_level_codes:
        return f"{product.display_name} is not sold in that unit."
    if new_level == old_level and quantity is None:
        return ""

    if quantity is None or quantity <= 0:
        quantity = cart_quantity(line["quantity"])
    else:
        line["quantity"] = format_qty(quantity)
    quantity_error = product.validate_quantity(quantity, new_level)
    if quantity_error:
        return quantity_error
    shortfall = stock_shortfall(product, quantity, new_level)
    if shortfall:
        return f"{product.display_name}: {shortfall}"

    old_standard = product.pack_price(old_level)
    new_standard = product.pack_price(new_level)
    price = to_decimal(line["price"])
    scheme_price = to_decimal(line.get("scheme_price"), None)
    if price != to_decimal(line.get("original_price"), old_standard) and price != scheme_price:
        new_price = quantize_money(price * product.pack_factor(new_level) / product.pack_factor(old_level))
    else:
        new_price = new_standard
    line.update({"unit": new_level, "price": str(new_price), "original_price": str(new_standard)})
    # A customer's price list rates the new unit afresh (pricing.services.till.apply_terms).
    line.pop("rate_key", None)
    cart[product_id] = line
    return ""


def _update_line_pricing(cart, product, posted_price, posted_discount, system_settings):
    """Apply a typed rate and line discount under the shop's pricing rules.

    Returns (saved, [(level, text), ...]); level is "error" or "success".
    """
    product_id = str(product.id)
    if product_id not in cart:
        return False, [("error", "Cart line no longer exists.")]

    updated_line = normalize_cart_item(cart[product_id])
    custom_price = updated_line["price"] if posted_price is None else posted_price
    line_discount = updated_line["discount_percent"] if posted_discount is None else posted_discount
    settings_messages = []

    if not system_settings.allow_price_override:
        custom_price = updated_line.get("original_price") or updated_line["price"]
        if posted_price is not None and safe_decimal(posted_price) != safe_decimal(custom_price):
            settings_messages.append("Price override is disabled in Settings.")

    if not system_settings.allow_line_discount:
        if posted_discount is not None and safe_decimal(posted_discount) != Decimal("0"):
            settings_messages.append("Line discount is disabled in Settings.")
        line_discount = "0"

    level = cart_level(product, updated_line)
    try:
        validated = validate_cart_line(product, custom_price, line_discount, level)
    except ValidationError as exc:
        return False, [("error", " ".join(exc.messages))]

    updated_line["price"] = str(validated["price"])
    updated_line["original_price"] = str(to_decimal(updated_line.get("original_price"), product.pack_price(level)))
    updated_line["discount_percent"] = str(validated["discount_percent"])
    cart[product_id] = updated_line
    if settings_messages:
        return True, [("error", " ".join(settings_messages))]
    return True, [("success", f"{product.display_name} pricing updated.")]


def _cart_state(cart, payment_method=Sale.CASH):
    cart = _normalize_cart(cart)
    product_ids = [int(product_id) for product_id in cart.keys()]
    products = Product.objects.in_bulk(product_ids)

    rows = []
    cart_total = Decimal("0")
    checkout_disabled = False
    stock_map = {}

    for product_id, item in cart.items():
        product = products.get(int(product_id))
        if product is None or not product.is_active:
            checkout_disabled = True
            rows.append(
                {
                    "product_id": product_id,
                    "name": item.get("name", "Unavailable Product"),
                    "quantity": cart_quantity(item["quantity"]),
                    "unit_price": to_decimal(item["price"]),
                    "original_price": to_decimal(item["original_price"], to_decimal(item["price"])),
                    "discount_percent": to_decimal(item["discount_percent"]),
                    "effective_unit_price": Decimal("0"),
                    "line_total": Decimal("0"),
                    "line_discount_amount": Decimal("0"),
                    "minimum_price": Decimal("0"),
                    "category_tax_rate": Decimal("0"),
                    "cash_category_tax_rate": Decimal("0"),
                    "bank_category_tax_rate": Decimal("0"),
                    "stock": 0,
                    "has_custom_price": False,
                    "has_line_discount": False,
                    "pricing_error": "This product is no longer available.",
                    "stock_error": "This product is no longer available.",
                    "line_id": str(product_id),
                    "is_free": False,
                }
            )
            continue

        quantity = cart_quantity(item["quantity"])
        level = cart_level(product, item)
        unit_price = to_decimal(item["price"])
        original_price = to_decimal(item["original_price"], unit_price)
        if level != normalize_pack_level(item.get("unit")):
            # The product's packing changed under a line already on the bill: the line falls to
            # a unit the product still has, at that unit's own price.
            unit_price = original_price = product.pack_price(level)
        unit_factor = product.pack_factor(level)
        pack_levels = product.pack_levels
        discount_percent = to_decimal(item["discount_percent"])
        final_unit_price = effective_unit_price(unit_price, discount_percent)
        minimum_price = product.minimum_sale_price_for_cost(line_unit_cost(product, level))
        line_total = cart_line_total(quantity, unit_price, discount_percent)
        gross_line_total = quantize_money(unit_price * quantity)
        line_discount_amount = quantize_money(gross_line_total - line_total)
        cash_category_tax_rate = category_tax_rate_for_product(product, Sale.CASH)
        bank_category_tax_rate = category_tax_rate_for_product(product, Sale.BANK)
        category_tax_rate = bank_category_tax_rate if payment_method == Sale.BANK else cash_category_tax_rate

        pricing_error = ""
        stock_error = ""
        try:
            validation = validate_cart_line(product, unit_price, discount_percent, level)
            final_unit_price = validation["final_unit_price"]
            minimum_price = validation["minimum_price"]
        except ValidationError as exc:
            pricing_error = " ".join(exc.messages)
            checkout_disabled = True

        quantity_error = product.validate_quantity(quantity, level)
        if quantity_error:
            stock_error = quantity_error
            checkout_disabled = True
        shortfall = stock_shortfall(product, quantity, level)
        if shortfall:
            stock_error = shortfall
            checkout_disabled = True

        cart_total += line_total
        stock_map[product_id] = product.stock
        by_piece = level == PACK_PIECE
        # Price lists and schemes (pricing app): the scheme a line earned, and a rate it set.
        scheme = line_scheme_fields(item, unit_price, original_price)
        rows.append(
            {
                **scheme,
                "line_id": str(product_id),
                "product_id": product_id,
                "product": product,
                "name": product.display_name,
                "quantity": quantity,
                "quantity_display": format_qty(quantity),
                # The unit the line is in: "Ctn", "Box", or the product's own unit.
                "unit": product.pack_label(level),
                "unit_level": level,
                "unit_factor": unit_factor,
                "base_quantity": product.to_base_quantity(quantity, level),
                # Offered in the line's unit dropdown; a plain product has only its own unit.
                "unit_options": pack_levels if len(pack_levels) > 1 else [],
                "per_unit": _per_unit(product, level),
                # The most of this unit the shelf can cover; None = no shelf limit.
                "max_quantity": (
                    None
                    if not tracks_stock(product)
                    else (product.stock if by_piece else (product.stock // unit_factor))
                ),
                "allows_fraction": product.allows_fractional_quantity and by_piece,
                "quantity_step": product.quantity_step if by_piece else "1",
                "unit_price": unit_price,
                "original_price": original_price,
                "discount_percent": discount_percent,
                "effective_unit_price": final_unit_price,
                "line_total": line_total,
                "line_discount_amount": line_discount_amount,
                "minimum_price": minimum_price,
                "category_tax_rate": category_tax_rate,
                "cash_category_tax_rate": cash_category_tax_rate,
                "bank_category_tax_rate": bank_category_tax_rate,
                # None = no shelf limit (services and other untracked items).
                "stock": product.stock if tracks_stock(product) else None,
                "has_custom_price": unit_price != original_price and scheme["scheme_price"] is None,
                "has_line_discount": discount_percent > 0,
                "pricing_error": pricing_error,
                "stock_error": stock_error,
            }
        )

    # Free goods of a scheme follow the line that earned them, at zero, as rows of their own.
    rows = with_free_goods(cart, rows)
    return {
        "cart": cart,
        "rows": rows,
        "cart_total": quantize_money(cart_total),
        "checkout_disabled": checkout_disabled,
        "stock_map": stock_map,
    }


@pos_access_required
@require_POST
@transaction.atomic
def scan_barcode(request, sku):
    code = (sku or "").strip()
    cart = request.session.get("cart", {})

    product = _find_product_by_code(code)
    if product is None:
        return JsonResponse({"status": "notfound"})

    if not _add_product_to_cart(cart, product):
        _save_cart(request, cart)
        return JsonResponse({"status": "out_of_stock", "product": product.display_name})

    _save_cart(request, cart)
    return JsonResponse({"status": "ok", "product_id": product.id})


@pos_access_required
def products_by_category(request, category_id):
    category = get_object_or_404(Category, pk=category_id, is_active=True)
    products = (
        Product.objects.filter(category=category, is_active=True)
        .select_related("brand", "category", "supplier")
        .order_by("name")
    )
    payload = [
        {
            "id": product.id,
            "name": product.display_name,
            "sku": product.sku,
            "barcode": product.barcode,
            "price": float(product.price or 0),
            "stock": product.stock,
            "location": product.location,
            "brand": product.brand.name if product.brand_id else "",
            "category": category.name,
            "supplier": product.supplier.name if product.supplier_id else "",
        }
        for product in products
    ]
    return JsonResponse({"category": category.name, "products": payload})


def _record_sale(
    *,
    request,
    system_settings,
    rows,
    locked_products,
    customer,
    is_credit,
    payment_method,
    bank_account,
    discount_rate,
    typed_discount=None,
    tax_rate,
):
    """Create the sale, its lines and its voucher. Callers wrap this in a savepoint.

    The rules live in pos.services.sale_recording.record_sale, which orders turned into
    invoices use too; the till adds who is selling and its own order booker choice.
    """
    return record_sale(
        user=request.user,
        system_settings=system_settings,
        rows=rows,
        locked_products=locked_products,
        customer=customer,
        is_credit=is_credit,
        payment_method=payment_method,
        bank_account=bank_account,
        discount_rate=discount_rate,
        typed_discount=typed_discount,
        tax_rate=tax_rate,
        # The order booker of this sale: the customer's own unless the till chose another for this bill.
        order_booker=sale_order_booker(request, customer),
        warn=lambda text: messages.warning(request, text),
        # The till's own name for the posting service, looked up now (tests replace it to make posting fail).
        post=post_sale_from_pos,
    )


@pos_access_required
@transaction.atomic
def pos_screen(request):
    system_settings = get_system_settings()
    cart = _normalize_cart(request.session.get("cart", {}))
    session_customer_id, session_credit = pop_customer_for_pos(request)
    bank_accounts = list(bank_account_queryset())
    active_bank_accounts = {str(account.id): account for account in bank_accounts}

    # The till keeps the sale's customer, payment and discount in the session while the bill has lines,
    # so walking to another screen and back does not lose them. An address with its own values wins.
    stored = {}
    if request.method == "GET" and cart and not any(key in request.GET for key in TILL_CONTEXT_PARAMS):
        stored = _clean_till_context(request.session.get(TILL_CONTEXT_KEY) or {}, system_settings)

    selected_category = request.GET.get("category", "").strip()
    selected_customer_id = request.GET.get("customer_id", "").strip() or request.POST.get("customer_id", "").strip()
    selected_bank_account_id = (
        request.GET.get("bank_account_id", "").strip()
        or request.POST.get("bank_account_id", "").strip()
        or stored.get("bank_account_id", "")
    )
    if not selected_customer_id and session_customer_id:
        selected_customer_id = str(session_customer_id)
    if not selected_customer_id:
        selected_customer_id = stored.get("customer_id", "")
    if selected_bank_account_id not in active_bank_accounts:
        selected_bank_account_id = ""

    valid_payment_methods = {choice[0] for choice in Sale.PAYMENT_METHOD_CHOICES}
    default_payment_method = system_settings.default_sale_payment_method or Sale.CASH
    payment_method = request.GET.get(
        "payment_method", request.POST.get("payment_method", stored.get("payment_method") or default_payment_method)
    )
    if payment_method not in valid_payment_methods:
        payment_method = default_payment_method if default_payment_method in valid_payment_methods else Sale.CASH

    discount_rate = request.GET.get("discount_rate", request.POST.get("discount_rate", stored.get("discount_rate", "0")))
    discount_type, discount_value = bill_discounts.typed_discount(request.GET, request.POST, stored)
    tax_rate = (
        request.GET.get("tax_rate", request.POST.get("tax_rate", stored.get("tax_rate", "0")))
        if system_settings.allows_manual_tax
        else "0"
    )
    prefill_credit_sale = (
        request.GET.get("credit") == "1"
        or request.POST.get("is_credit") == "on"
        or session_credit
        or bool(stored.get("credit"))
    )
    if not system_settings.allow_credit_sales:
        prefill_credit_sale = False

    def pos_redirect_url():
        return _build_pos_url(
            category=selected_category,
            customer_id=selected_customer_id,
            credit=prefill_credit_sale,
            payment_method=payment_method,
            bank_account_id=selected_bank_account_id,
            discount_rate=discount_rate,
            discount_type=discount_type,
            discount_value=discount_value,
            tax_rate=tax_rate,
        )

    def _held_context():
        return {
            "customer_id": str(selected_customer_id or ""),
            "credit": bool(prefill_credit_sale),
            "payment_method": payment_method,
            "bank_account_id": selected_bank_account_id,
            "discount_rate": str(discount_rate or ""),
            "discount_type": discount_type,
            "discount_value": str(discount_value or ""),
            "tax_rate": str(tax_rate or ""),
        }

    wants_till_json = request.method == "POST" and request.POST.get("respond") == "json"

    def _current_till_context():
        return _clean_till_context(
            {
                "customer_id": selected_customer_id,
                "credit": prefill_credit_sale,
                "payment_method": payment_method,
                "bank_account_id": selected_bank_account_id,
                "discount_rate": discount_rate,
                "discount_type": discount_type,
                "discount_value": discount_value,
                "tax_rate": tax_rate,
            },
            system_settings,
        )

    def _json_refusal(**extra):
        # A refused checkout: the reasons queued so far go back with the bill as it now stands.
        refusals = [str(message) for message in messages.get_messages(request)]
        payload = _till_payload(
            request,
            system_settings,
            cart,
            _current_till_context(),
            ok=False,
            status="refused",
            notice={"kind": "out", "text": " ".join(refusals) or "The sale was not saved."},
        )
        payload.update(extra)
        return JsonResponse(payload)

    if request.method == "POST":
        action = request.POST.get("action")
        product_id = request.POST.get("product_id", "").strip()
        selected_customer_id = (request.POST.get("selected_customer_id") or request.POST.get("customer_id") or selected_customer_id).strip()
        selected_bank_account_id = (request.POST.get("preserve_bank_account_id") or request.POST.get("bank_account_id") or selected_bank_account_id).strip()
        if selected_bank_account_id not in active_bank_accounts:
            selected_bank_account_id = ""
        prefill_credit_sale = system_settings.allow_credit_sales and (
            request.POST.get("preserve_credit") == "1" or request.POST.get("is_credit") == "on"
        )
        payment_method = request.POST.get("preserve_payment_method", request.POST.get("payment_method", payment_method))
        if payment_method not in valid_payment_methods:
            payment_method = default_payment_method if default_payment_method in valid_payment_methods else Sale.CASH
        discount_rate = request.POST.get("preserve_discount_rate", request.POST.get("discount_rate", discount_rate))
        discount_type, discount_value = bill_discounts.typed_discount(request.POST, request.GET, stored)
        tax_rate = request.POST.get("preserve_tax_rate", request.POST.get("tax_rate", tax_rate)) if system_settings.allows_manual_tax else "0"
        selected_category = request.POST.get("selected_category", selected_category).strip()

        if action == "add" and product_id:
            product = get_object_or_404(Product, id=product_id, is_active=True)
            if not _add_product_to_cart(cart, product):
                messages.error(request, f"{product.display_name} has no more stock available.")
            _save_cart(request, cart)
            return redirect(pos_redirect_url())

        if action == "decrease" and product_id:
            if product_id in cart:
                _decrease_cart_line(cart, product_id)
                _save_cart(request, cart)
            return redirect(pos_redirect_url())

        if action == "set_quantity" and product_id:
            product = get_object_or_404(Product, id=product_id, is_active=True)
            quantity_error = _set_line_quantity(cart, product, cart_quantity(request.POST.get("quantity")))
            if quantity_error:
                messages.error(request, quantity_error)
            _save_cart(request, cart)
            return redirect(pos_redirect_url())

        if action == "remove_all" and product_id:
            cart.pop(product_id, None)
            _save_cart(request, cart)
            return redirect(pos_redirect_url())

        if action == "update_line" and product_id:
            product = get_object_or_404(Product, id=product_id, is_active=True)
            saved, notes = _update_line_pricing(
                cart,
                product,
                request.POST.get("custom_price"),
                request.POST.get("line_discount"),
                system_settings,
            )
            if saved:
                _save_cart(request, cart)
            for level, text in notes:
                (messages.success if level == "success" else messages.error)(request, text)
            return redirect(pos_redirect_url())

        if action in {"hold_cart", "resume_cart", "discard_held"} and not module_enabled("hold_sale"):
            return module_disabled_response(request, "hold_sale")

        if action == "hold_cart":
            customer_name = ""
            if selected_customer_id:
                customer_name = (
                    Customer.objects.filter(id=selected_customer_id).values_list("name", flat=True).first() or ""
                )
            try:
                held = hold_cart(
                    request.session,
                    cart,
                    label=request.POST.get("hold_label") or customer_name,
                    context=_held_context(),
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(pos_redirect_url())
            _save_cart(request, {})
            _store_till_context(request, None)
            messages.success(request, f"'{held['label']}' is on hold. The till is ready for the next customer.")
            # Customer, discount and payment belong to the parked sale, not the next one.
            return redirect(_build_pos_url(category=selected_category))

        if action == "resume_cart":
            held = pop_held_cart(request.session, request.POST.get("held_id", ""))
            if held is None:
                messages.error(request, "That held sale is no longer available.")
                return redirect(pos_redirect_url())
            if cart:
                # Never lose the sale on screen: it swaps places with the one being resumed
                # (popping first freed a slot, so this cannot hit the held-sales limit).
                parked = hold_cart(request.session, cart, context=_held_context())
                messages.info(request, f"The sale you were working on is now held as '{parked['label']}'.")
            _save_cart(request, held["cart"])
            context = held.get("context") or {}
            messages.success(request, f"Resumed '{held['label']}'.")
            return redirect(
                _build_pos_url(
                    category=selected_category,
                    customer_id=context.get("customer_id"),
                    credit=bool(context.get("credit")) and system_settings.allow_credit_sales,
                    payment_method=context.get("payment_method") if context.get("payment_method") in valid_payment_methods else None,
                    bank_account_id=context.get("bank_account_id") if context.get("bank_account_id") in active_bank_accounts else None,
                    discount_rate=context.get("discount_rate"),
                    discount_type=context.get("discount_type"),
                    discount_value=context.get("discount_value"),
                    tax_rate=context.get("tax_rate") if system_settings.allows_manual_tax else None,
                )
            )

        if action == "discard_held":
            held = pop_held_cart(request.session, request.POST.get("held_id", ""))
            if held is None:
                messages.error(request, "That held sale is no longer available.")
            else:
                messages.success(request, f"Held sale '{held['label']}' discarded.")
            return redirect(pos_redirect_url())

        if action == "checkout":
            if not cart:
                messages.error(request, "Add at least one item before checkout.")
            else:
                # The bill is priced for the customer it is sold to, and its schemes worked out, before it is read.
                apply_terms(cart, selected_customer_id)
                product_ids = [int(pid) for pid in cart.keys()] + free_product_ids(cart)
                locked_products = Product.objects.select_for_update().in_bulk(product_ids)
                cart_snapshot = _cart_state(cart, payment_method=payment_method)

                if cart_snapshot["checkout_disabled"]:
                    messages.error(request, "Review the cart lines with stock or pricing issues before checkout.")
                else:
                    for row in cart_snapshot["rows"]:
                        if row.get("is_free"):
                            # Free goods were fitted to the shelf left after the paid lines.
                            continue
                        product = locked_products.get(int(row["product_id"]))
                        if product is None or stock_shortfall(product, row["quantity"], row.get("unit_level") or PACK_PIECE):
                            messages.error(request, f"Stock changed for {row['name']}. Please review the cart.")
                            if wants_till_json:
                                return _json_refusal()
                            return redirect(pos_redirect_url())

                    discount_rate_value = _normalized_rate(request.POST.get("discount_rate"))
                    tax_rate_value = _normalized_rate(request.POST.get("tax_rate")) if system_settings.allows_manual_tax else Decimal("0")
                    customer = (
                        Customer.objects.filter(id=selected_customer_id, is_active=True).first()
                        if selected_customer_id
                        else None
                    )
                    is_credit = request.POST.get("is_credit") == "on"
                    payment_method_value = request.POST.get("payment_method") or default_payment_method
                    if payment_method_value not in valid_payment_methods:
                        payment_method_value = default_payment_method if default_payment_method in valid_payment_methods else Sale.CASH
                    bank_account = active_bank_accounts.get(selected_bank_account_id) if payment_method_value == Sale.BANK else None
                    # The proof of a bank payment travels with the checkout; cash and credit sales carry none.
                    bank_receipt, bank_receipt_problem = request_upload(
                        request, bank_payment=payment_method_value == Sale.BANK and not is_credit
                    )

                    if is_credit and not system_settings.allow_credit_sales:
                        messages.error(request, "Credit sale is disabled in Settings.")
                    elif is_credit and customer is None:
                        messages.error(request, "Credit sale requires an active customer.")
                    elif bank_receipt_problem:
                        messages.error(request, bank_receipt_problem)
                        if wants_till_json:
                            return _json_refusal(receipt_error=bank_receipt_problem)
                    else:
                        try:
                            # Savepoint: a failure below must undo the sale, its stock movement and
                            # any voucher lines, while the cashier still gets a page with the reason.
                            with transaction.atomic():
                                sale, settings_adjusted_lines = _record_sale(
                                    request=request,
                                    system_settings=system_settings,
                                    rows=cart_snapshot["rows"],
                                    locked_products=locked_products,
                                    customer=customer,
                                    is_credit=is_credit,
                                    payment_method=payment_method_value,
                                    bank_account=bank_account,
                                    discount_rate=discount_rate_value,
                                    typed_discount=bill_discounts.typed_discount(request.POST),
                                    tax_rate=tax_rate_value,
                                )
                                attach_receipt(sale, bank_receipt, actor=request.user, audit=False)
                        except CheckoutRefused as exc:
                            messages.error(request, str(exc))
                        except ValidationError as exc:
                            messages.error(request, f"Sale not saved. {' '.join(exc.messages)}")
                        except ValueError as exc:
                            logger.exception("POS checkout could not be posted to the ledger")
                            messages.error(request, f"Sale not saved, nothing was charged or taken from stock. {exc}")
                        else:
                            request.session["cart"] = {}
                            request.session.modified = True
                            _store_till_context(request, None)
                            if settings_adjusted_lines:
                                messages.warning(
                                    request,
                                    "Some saved cart price changes were removed because current Settings no longer allow them.",
                                )
                            messages.success(request, f"Sale {sale.display_number} completed successfully.")
                            if wants_till_json:
                                return JsonResponse(
                                    {
                                        "ok": True,
                                        "status": "sold",
                                        "stamp": "Credit" if sale.is_credit else "Paid",
                                        "sale": sale.display_number,
                                        "redirect": reverse("invoice", kwargs={"sale_id": sale.id}),
                                    }
                                )
                            return redirect("invoice", sale_id=sale.id)

    till_context = _current_till_context()
    apply_terms(cart, till_context["customer_id"])
    _save_cart(request, cart)
    if wants_till_json:
        return _json_refusal()
    if request.method == "GET":
        _store_till_context(request, till_context if cart else None)

    cart_state = _cart_state(cart, payment_method=payment_method)
    bill_discount = _bill_discount(till_context, cart_state)
    discount_rate = bill_discount.rate
    sale_preview = sale_totals_from_rows(cart_state["rows"], discount_rate, tax_rate, system_settings, payment_method=payment_method)

    products_queryset = Product.objects.filter(is_active=True)
    if selected_category:
        products_queryset = products_queryset.filter(category_id=selected_category)

    products = list(products_queryset.select_related("category", "brand").order_by("name"))
    for product in products:
        # The browse list prices a product in the unit it goes on the bill in.
        till_level = (
            product.resolve_pack_level(system_settings.till_default_unit) if product.has_packaging else PACK_PIECE
        )
        product.till_price = product.pack_price(till_level)
        product.till_per_unit = _per_unit(product, till_level)
    categories = Category.objects.filter(is_active=True).order_by("name")
    customers = Customer.objects.filter(is_active=True).order_by("name")

    return render(
        request,
        "pos/pos_screen.html",
        {
            "products": products,
            "categories": categories,
            "customers": customers,
            "bank_accounts": bank_accounts,
            "selected_category": str(selected_category) if selected_category else "",
            "cart": cart_state["cart"],
            "cart_rows": cart_state["rows"],
            "cart_total": cart_state["cart_total"],
            "stock_map": cart_state["stock_map"],
            "checkout_disabled": cart_state["checkout_disabled"],
            "selected_customer_id": str(selected_customer_id) if selected_customer_id else "",
            "selected_bank_account_id": selected_bank_account_id,
            "prefill_credit_sale": prefill_credit_sale,
            "payment_method_value": payment_method,
            "discount_rate_value": _normalized_rate(discount_rate),
            "bill_discount": bill_discount,
            "tax_rate_value": _normalized_rate(tax_rate) if system_settings.allows_manual_tax else Decimal("0"),
            "tax_label_value": system_settings.tax_label,
            "manual_tax_enabled": system_settings.allows_manual_tax,
            "uses_payment_method_gst": system_settings.uses_payment_method_gst,
            "uses_category_gst": system_settings.uses_category_gst,
            "gst_enabled": system_settings.gst_enabled,
            "cash_gst_rate_value": system_settings.gst_rate_for_payment_method(Sale.CASH),
            "bank_gst_rate_value": system_settings.gst_rate_for_payment_method(Sale.BANK),
            "allow_credit_sales": system_settings.allow_credit_sales,
            "allow_price_override": system_settings.allow_price_override,
            "allow_line_discount": system_settings.allow_line_discount,
            "sale_preview": sale_preview,
            "pos_customer_add_url": build_customer_add_url(),
            "held_carts": list_held_carts(request.session) if module_enabled("hold_sale") else [],
            "selected_line_id": _last_paid_line(cart_state["rows"]),
            "till_customer": _till_customer(till_context["customer_id"]),
            "tax_summary_label": _tax_summary_label(system_settings, payment_method),
            "till_customer_form": CustomerForm(prefix=None) if module_enabled("parties") else None,
        },
    )


# ---------------------------------------------------------------------------
# The till's JSON endpoints: the same cart rules as the form actions of
# pos_screen, answered with the whole bill so the page never reloads.
# ---------------------------------------------------------------------------

TILL_CONTEXT_KEY = "till_context"
TILL_CONTEXT_PARAMS = ("customer_id", "credit", "payment_method", "bank_account_id", "discount_rate", "tax_rate")
TILL_CONTEXT_PARAMS += ("discount_type", "discount_value")
TILL_UNIT_FACTORS = {
    ("g", "kg"): Decimal("0.001"),
    ("kg", "g"): Decimal("1000"),
    ("ml", "l"): Decimal("0.001"),
    ("l", "ml"): Decimal("1000"),
}


def _valid_payment_method(value, system_settings):
    valid = {choice[0] for choice in Sale.PAYMENT_METHOD_CHOICES}
    if value in valid:
        return value
    default = system_settings.default_sale_payment_method or Sale.CASH
    return default if default in valid else Sale.CASH


def _blank_till_context(system_settings):
    return {
        "customer_id": "",
        "credit": False,
        "payment_method": _valid_payment_method(None, system_settings),
        "bank_account_id": "",
        "discount_rate": "0",
        "discount_type": bill_discounts.PERCENT,
        "discount_value": "0",
        "tax_rate": "0",
    }


def _clean_till_context(values, system_settings):
    """Customer, payment and discount of the sale on screen, held to what Settings allow."""
    context = _blank_till_context(system_settings)
    customer_id = str(values.get("customer_id") or "").strip()
    if customer_id.isdigit() and Customer.objects.filter(id=customer_id, is_active=True).exists():
        context["customer_id"] = customer_id
    context["credit"] = bool(values.get("credit")) and bool(system_settings.allow_credit_sales)
    context["payment_method"] = _valid_payment_method(values.get("payment_method"), system_settings)
    bank_account_id = str(values.get("bank_account_id") or "").strip()
    if (
        bank_account_id.isdigit()
        and context["payment_method"] == Sale.BANK
        and not context["credit"]
        and bank_account_queryset().filter(id=bank_account_id).exists()
    ):
        context["bank_account_id"] = bank_account_id
    context["discount_rate"] = str(_normalized_rate(values.get("discount_rate")))
    context.update(bill_discounts.clean_discount_context(values))
    if system_settings.allows_manual_tax:
        context["tax_rate"] = str(_normalized_rate(values.get("tax_rate")))
    return context


def _read_till_context(request, system_settings):
    """The till posts the sale's context with every cart change; anything it leaves out keeps its stored value."""
    values = dict(request.session.get(TILL_CONTEXT_KEY) or {})
    for key in TILL_CONTEXT_PARAMS:
        if key in request.POST:
            values[key] = request.POST.get(key, "")
    if "credit" in request.POST:
        values["credit"] = request.POST.get("credit") in {"1", "on", "true"}
    if "discount_rate" in request.POST and "discount_type" not in request.POST:
        # A caller that posts only a rate means a percentage, as it always has.
        values["discount_type"] = bill_discounts.PERCENT
    return _clean_till_context(values, system_settings)


def _store_till_context(request, context):
    if context is None:
        request.session.pop(TILL_CONTEXT_KEY, None)
    else:
        request.session[TILL_CONTEXT_KEY] = context
    request.session.modified = True


def _till_customer(customer_id):
    if not customer_id:
        return None
    customer = Customer.objects.filter(id=customer_id, is_active=True).first()
    if customer is None:
        return None
    badge = get_customer_badge(customer)
    return {
        "id": customer.id,
        "name": customer.name,
        "balance_type": badge["balance_type"],
        "balance": format_money(badge["amount"]),
        # Credit limit line and the customer's usual order booker.
        **till_customer_terms(customer, badge),
    }


def _last_paid_line(rows):
    """The line the till selects by default: the last one added, never a scheme's free goods."""
    for row in reversed(rows):
        if not row.get("is_free"):
            return str(row["line_id"])
    return str(rows[-1]["line_id"]) if rows else ""


def _bill_context(request, system_settings, cart_state, sale_preview, context, selected_line_id=""):
    """What the bill, held-bill and totals partials read; pos_screen renders the same names."""
    rows = cart_state["rows"]
    if not selected_line_id or not any(str(row["line_id"]) == str(selected_line_id) for row in rows):
        selected_line_id = _last_paid_line(rows)
    return {
        "cart_rows": rows,
        "cart_total": cart_state["cart_total"],
        "checkout_disabled": cart_state["checkout_disabled"],
        "selected_line_id": str(selected_line_id),
        "sale_preview": sale_preview,
        "payment_method_value": context["payment_method"],
        "tax_label_value": system_settings.tax_label,
        "manual_tax_enabled": system_settings.allows_manual_tax,
        "uses_payment_method_gst": system_settings.uses_payment_method_gst,
        "uses_category_gst": system_settings.uses_category_gst,
        "gst_enabled": system_settings.gst_enabled,
        "cash_gst_rate_value": system_settings.gst_rate_for_payment_method(Sale.CASH),
        "bank_gst_rate_value": system_settings.gst_rate_for_payment_method(Sale.BANK),
        "allow_price_override": system_settings.allow_price_override,
        "allow_line_discount": system_settings.allow_line_discount,
        "held_carts": list_held_carts(request.session) if module_enabled("hold_sale") else [],
        "till_customer": _till_customer(context["customer_id"]),
    }


def _tax_summary_label(system_settings, payment_method):
    """The tax line of the totals names what it follows: the payment method, the category table, or nothing."""
    method_label = "Bank" if payment_method == Sale.BANK else "Cash"
    if system_settings.uses_payment_method_gst:
        rate = system_settings.gst_rate_for_payment_method(payment_method)
        return f"{method_label} {system_settings.tax_label} {rate:.2f}%"
    if system_settings.uses_category_gst:
        return f"{method_label} {system_settings.tax_label}"
    return system_settings.tax_label


def _bill_discount(context, cart_state):
    """The bill's discount worked out on the lines now on it.

    Writes the rate it comes to into the context, which is what the totals are calculated with.
    A cash discount that no longer fits the bill is not trimmed: it blocks checkout until it is changed.
    """
    rows = cart_state["rows"]
    subtotal = sum((Decimal(row["line_total"]) for row in rows), Decimal("0"))
    kind = context.get("discount_type")
    typed = context.get("discount_value") if kind == bill_discounts.AMOUNT else context.get("discount_rate")
    discount = bill_discounts.resolve_bill_discount(kind, typed, subtotal, has_lines=bool(rows))
    context["discount_rate"] = str(discount.rate)
    if discount.error:
        cart_state["checkout_disabled"] = True
    return discount


def _till_payload(request, system_settings, cart, context, *, ok=True, status="ok", notice=None, touched=""):
    cart_state = _cart_state(cart, payment_method=context["payment_method"])
    bill_discount = _bill_discount(context, cart_state)
    if bill_discount.error and not notice:
        notice = {"kind": "out", "text": bill_discount.error}
    preview = sale_totals_from_rows(
        cart_state["rows"],
        context["discount_rate"],
        context["tax_rate"],
        system_settings,
        payment_method=context["payment_method"],
    )
    bill = _bill_context(request, system_settings, cart_state, preview, context, selected_line_id=touched)
    return {
        "ok": ok,
        "status": status,
        "notice": notice,
        "selected": bill["selected_line_id"],
        "lines": [
            {
                "product_id": str(row["line_id"]),
                "is_free": bool(row.get("is_free")),
                "scheme": row.get("scheme_name", ""),
                "name": row["name"],
                "quantity": format_qty(row["quantity"]),
                "unit": row.get("unit", ""),
                "unit_level": row.get("unit_level", PACK_PIECE),
                "units": [
                    {"code": option["code"], "label": option["label"], "factor": format_qty(option["factor"])}
                    for option in row.get("unit_options", [])
                ],
                "rate": str(quantize_money(row["unit_price"])),
                "discount_percent": str(quantize_money(row["discount_percent"])),
                "amount": str(quantize_money(row["line_total"])),
                "pricing_error": row["pricing_error"],
                "stock_error": row["stock_error"],
            }
            for row in cart_state["rows"]
        ],
        "totals": {
            "subtotal": str(preview["subtotal"]),
            "line_discount": str(preview["line_discount_total"]),
            "discount": str(preview["discount_amount"]),
            "tax": str(preview["tax_amount"]),
            "total": str(preview["total_amount"]),
            "subtotal_display": format_money(preview["subtotal"]),
            "line_discount_display": format_money(preview["line_discount_total"]),
            "discount_display": format_money(preview["discount_amount"]),
            "tax_display": format_money(preview["tax_amount"]),
            "total_display": format_amount(preview["total_amount"]),
            "tax_label": _tax_summary_label(system_settings, context["payment_method"]),
            "comparison": [
                {
                    "payment_method": option["payment_method"],
                    "label": option["tax_label_display"],
                    "method_label": option["label"],
                    "total_display": format_money(option["total_amount"]),
                }
                for option in preview["comparison_totals"]
            ],
        },
        "checkout_disabled": cart_state["checkout_disabled"],
        "discount": bill_discounts.till_discount_payload(bill_discount),
        "held": [
            {"id": held["id"], "label": held["label"], "total": str(held["total"]), "line_count": held["line_count"]}
            for held in bill["held_carts"]
        ],
        "context": context,
        "customer": bill["till_customer"],
        "html": {
            "bill": render_to_string("pos/partials/till_bill.html", bill, request=request),
            "held": render_to_string("pos/partials/till_held.html", bill, request=request),
        },
    }


def _quantity_in_product_unit(product, quantity, typed_unit):
    """500 g of a product kept in kg is 0.5. Returns (quantity, reason it cannot be converted)."""
    typed_unit = (typed_unit or "").strip().lower()
    product_unit = (product.unit or "").lower()
    if not typed_unit or typed_unit == product_unit:
        return quantity, ""
    factor = TILL_UNIT_FACTORS.get((typed_unit, product_unit))
    if factor is None:
        return quantity, f"{product.display_name} is sold by the {product.unit}, not by {typed_unit}."
    return cart_quantity(quantity * factor), ""


# A quantity typed with a pack word on the scan line: "5 ctn", "2 box", "12 pcs".
TILL_PACK_WORDS = {
    "ctn": "carton",
    "ctns": "carton",
    "carton": "carton",
    "cartons": "carton",
    "box": "box",
    "boxes": "box",
    "bx": "box",
    "pc": PACK_PIECE,
    "pcs": PACK_PIECE,
    "piece": PACK_PIECE,
    "pieces": PACK_PIECE,
}


def _typed_pack_level(product, typed_unit):
    """The pack level a typed unit word names for this product, or None when it is not a pack word."""
    typed_unit = (typed_unit or "").strip().lower()
    level = TILL_PACK_WORDS.get(typed_unit)
    if level is None or not product.has_packaging:
        return None
    return level


def _till_add(request, cart, product, typed_quantity):
    """One more of the product, or the typed quantity on top of what the line already holds."""
    product_id = str(product.id)
    typed_quantity = (typed_quantity or "").strip()
    if not typed_quantity:
        if _add_product_to_cart(cart, product):
            return {"touched": product_id}
        return {
            "ok": False,
            "status": "out_of_stock",
            "touched": product_id if product_id in cart else "",
            "notice": {"kind": "out", "text": f"{product.display_name} has no more stock available."},
        }

    quantity = cart_quantity(typed_quantity)
    if quantity <= 0:
        return {"ok": False, "status": "invalid", "notice": {"kind": "out", "text": "Quantity must be greater than zero."}}

    was_on_bill = product_id in cart
    typed_unit = request.POST.get("quantity_unit")
    typed_level = _typed_pack_level(product, typed_unit)
    if typed_level is not None:
        # "5 ctn": the line is put in that unit; a line already on the bill in another unit is refused
        # rather than silently converted.
        if typed_level not in product.pack_level_codes:
            return {
                "ok": False,
                "status": "invalid",
                "notice": {"kind": "out", "text": f"{product.display_name} is not sold in that unit."},
            }
        if was_on_bill and cart_level(product, cart[product_id]) != typed_level:
            return {
                "ok": False,
                "status": "invalid",
                "touched": product_id,
                "notice": {
                    "kind": "out",
                    "text": f"{product.display_name} is already on the bill in another unit. Change the unit on its line first.",
                },
            }
        if not was_on_bill:
            cart[product_id] = seed_cart_item(product, typed_level)
            cart[product_id]["quantity"] = "0"
    else:
        quantity, unit_error = _quantity_in_product_unit(product, quantity, typed_unit)
        if unit_error:
            return {"ok": False, "status": "invalid", "notice": {"kind": "out", "text": unit_error}}

    already = cart_quantity(cart[product_id]["quantity"]) if was_on_bill else Decimal("0")
    error = _set_line_quantity(cart, product, already + quantity)
    if error:
        if not was_on_bill:
            cart.pop(product_id, None)
        return {
            "ok": False,
            "status": "invalid",
            "touched": product_id if was_on_bill else "",
            "notice": {"kind": "out", "text": error},
        }
    return {"touched": product_id}


def _till_product(request):
    product_id = (request.POST.get("product_id") or "").strip()
    if not product_id.isdigit():
        return None
    return Product.objects.select_for_update().filter(id=product_id, is_active=True).first()


def _product_gone():
    return {
        "ok": False,
        "status": "notfound",
        "http_status": 404,
        "notice": {"kind": "out", "text": "That product is no longer available."},
    }


def _till_do_add(request, system_settings, cart, context):
    product = _till_product(request)
    if product is None:
        return _product_gone()
    return _till_add(request, cart, product, request.POST.get("quantity"))


def _till_do_scan(request, system_settings, cart, context):
    code = (request.POST.get("code") or "").strip()
    if not code:
        return {"ok": False, "status": "invalid", "notice": {"kind": "out", "text": "Scan or type a code first."}}
    product = _find_product_by_code(code)
    if product is None:
        return {
            "ok": False,
            "status": "notfound",
            "notice": {
                "kind": "due",
                "text": f'No product matches "{code}".',
                "code": code,
                "can_create": user_can_access_module(request.user, "products", ACCESS_MANAGE),
            },
        }
    return _till_add(request, cart, product, request.POST.get("quantity"))


def _till_do_increase(request, system_settings, cart, context):
    product = _till_product(request)
    if product is None:
        return _product_gone()
    return _till_add(request, cart, product, "")


def _till_do_decrease(request, system_settings, cart, context):
    product_id = (request.POST.get("product_id") or "").strip()
    _decrease_cart_line(cart, product_id)
    return {"touched": product_id if product_id in cart else ""}


def _till_do_quantity(request, system_settings, cart, context):
    product = _till_product(request)
    if product is None:
        return _product_gone()
    product_id = str(product.id)
    error = _set_line_quantity(cart, product, cart_quantity(request.POST.get("quantity")))
    result = {"touched": product_id if product_id in cart else ""}
    if error:
        result.update({"ok": False, "status": "invalid", "notice": {"kind": "out", "text": error}})
    return result


def _till_do_unit(request, system_settings, cart, context):
    """The unit dropdown on a bill line: carton, box or piece."""
    product = _till_product(request)
    if product is None:
        return _product_gone()
    product_id = str(product.id)
    # "5" typed in the quantity box, then the unit chosen: both arrive together.
    typed_quantity = (request.POST.get("quantity") or "").strip()
    error = _set_line_unit(
        cart, product, request.POST.get("unit"), cart_quantity(typed_quantity) if typed_quantity else None
    )
    result = {"touched": product_id if product_id in cart else ""}
    if error:
        result.update({"ok": False, "status": "invalid", "notice": {"kind": "out", "text": error}})
    return result


def _till_do_price(request, system_settings, cart, context):
    product = _till_product(request)
    if product is None:
        return _product_gone()
    saved, notes = _update_line_pricing(
        cart,
        product,
        request.POST.get("custom_price"),
        request.POST.get("line_discount"),
        system_settings,
    )
    result = {"touched": str(product.id)}
    refusals = [text for level, text in notes if level == "error"]
    if refusals:
        result.update(
            {"ok": False, "status": "ok" if saved else "invalid", "notice": {"kind": "out", "text": " ".join(refusals)}}
        )
    return result


def _till_do_remove(request, system_settings, cart, context):
    line_id = (request.POST.get("product_id") or "").strip()
    if is_free_line_id(line_id):
        refusal = decline_free_goods(cart, line_id)
        if refusal:
            return {"ok": False, "status": "invalid", "touched": line_id, "notice": {"kind": "out", "text": refusal}}
        return {}
    cart.pop(line_id, None)
    return {}


def _till_do_clear(request, system_settings, cart, context):
    return {"cart": {}, "context": _blank_till_context(system_settings)}


def _till_do_context(request, system_settings, cart, context):
    return {"touched": (request.POST.get("selected") or "").strip()}


def _till_do_hold(request, system_settings, cart, context):
    customer_name = ""
    if context["customer_id"]:
        customer_name = Customer.objects.filter(id=context["customer_id"]).values_list("name", flat=True).first() or ""
    try:
        held = hold_cart(request.session, cart, label=request.POST.get("hold_label") or customer_name, context=context)
    except ValueError as exc:
        return {"ok": False, "status": "invalid", "notice": {"kind": "out", "text": str(exc)}}
    return {
        "cart": {},
        "context": _blank_till_context(system_settings),
        "notice": {"kind": "in", "text": f"'{held['label']}' is on hold. The till is ready for the next customer."},
    }


def _till_do_resume(request, system_settings, cart, context):
    held = pop_held_cart(request.session, request.POST.get("held_id", ""))
    if held is None:
        return {"ok": False, "status": "notfound", "notice": {"kind": "out", "text": "That held sale is no longer available."}}
    text = f"Resumed '{held['label']}'."
    if cart:
        # Never lose the sale on screen: it swaps places with the one being resumed.
        parked = hold_cart(request.session, cart, context=context)
        text = f"{text} The sale you were working on is now held as '{parked['label']}'."
    return {
        "cart": held["cart"],
        "context": _clean_till_context(held.get("context") or {}, system_settings),
        "notice": {"kind": "in", "text": text},
    }


def _till_do_discard(request, system_settings, cart, context):
    held = pop_held_cart(request.session, request.POST.get("held_id", ""))
    if held is None:
        return {"ok": False, "status": "notfound", "notice": {"kind": "out", "text": "That held sale is no longer available."}}
    return {"notice": {"kind": "in", "text": f"Held sale '{held['label']}' discarded."}}


TILL_ACTIONS = {
    "add": _till_do_add,
    "scan": _till_do_scan,
    "increase": _till_do_increase,
    "decrease": _till_do_decrease,
    "quantity": _till_do_quantity,
    "unit": _till_do_unit,
    "price": _till_do_price,
    "remove": _till_do_remove,
    "clear": _till_do_clear,
    "context": _till_do_context,
    "hold": _till_do_hold,
    "resume": _till_do_resume,
    "discard": _till_do_discard,
}
TILL_HOLD_ACTIONS = {"hold", "resume", "discard"}


@pos_access_required
@require_POST
@transaction.atomic
def till_action(request, action):
    handler = TILL_ACTIONS.get(action)
    if handler is None:
        return JsonResponse({"ok": False, "status": "error", "message": "The till does not know that action."}, status=404)
    if action in TILL_HOLD_ACTIONS and not module_enabled("hold_sale"):
        return module_disabled_response(request, "hold_sale")

    system_settings = get_system_settings()
    cart = _normalize_cart(request.session.get("cart", {}))
    context = _read_till_context(request, system_settings)
    if is_free_line_id(request.POST.get("product_id")) and action not in ("remove", "context"):
        # A free-goods line follows its scheme: only taking it off (when Settings allow) applies to it.
        result = {
            "ok": False,
            "status": "invalid",
            "touched": request.POST.get("product_id"),
            "notice": {"kind": "out", "text": free_line_refusal()},
        }
    else:
        result = handler(request, system_settings, cart, context)

    cart = _normalize_cart(result.get("cart", cart))
    context = result.get("context", context)
    # Re-price for the sale's customer and work out the schemes after every change to the bill.
    apply_terms(cart, context["customer_id"])
    _save_cart(request, cart)
    # An empty bill keeps no customer or discount: the next sale starts clean.
    _store_till_context(request, context if cart else None)
    payload = _till_payload(
        request,
        system_settings,
        cart,
        context,
        ok=result.get("ok", True),
        status=result.get("status", "ok"),
        notice=result.get("notice"),
        touched=result.get("touched", ""),
    )
    if action == "context" and payload["discount"]["error"]:
        # The typed discount is kept so the cashier can see and correct it; the bill cannot close meanwhile.
        payload.update({"ok": False, "status": "invalid"})
    return JsonResponse(payload, status=result.get("http_status", 200))


def _per_unit(product, level=PACK_PIECE):
    unit = product.pack_label(level) or ""
    return "" if not unit or unit == Product.UNIT_PIECE else f" / {unit}"


@pos_access_required
@require_GET
def till_catalogue(request):
    """The active catalogue for the scan line's suggestions and the browse list, fetched once per visit."""
    products = list(
        Product.objects.filter(is_active=True).select_related("category", "brand").order_by("name", "price", "variant_label")
    )
    # Sizes of one item: typing the name offers the family once, and the picker lists its sizes.
    # With the module off every size is simply a product of its own, named "Bread - Small".
    grouping = module_enabled("variants")
    families = {}
    if grouping:
        for product in products:
            if product.variant_group_id and product.variant_label:
                family = families.setdefault(
                    product.variant_group_id, {"id": product.variant_group_id, "name": product.name, "sizes": []}
                )
                family["sizes"].append(product.id)
    # Prices in the suggestions are per the unit the product goes on the bill in.
    till_unit = get_system_settings().till_default_unit
    sale_levels = {
        product.id: product.resolve_pack_level(till_unit) if product.has_packaging else PACK_PIECE for product in products
    }
    return JsonResponse(
        {
            "families": list(families.values()),
            "products": [
                {
                    "id": product.id,
                    "name": product.display_name,
                    "family": product.variant_group_id if product.variant_group_id in families and product.variant_label else 0,
                    "label": product.variant_label if product.variant_group_id in families else "",
                    "sku": product.sku or "",
                    "barcode": product.barcode or "",
                    "unit": product.unit or "",
                    "fraction": product.allows_fractional_quantity,
                    "price": (
                        f"{format_money(product.pack_price(sale_levels[product.id]))}"
                        f"{_per_unit(product, sale_levels[product.id])}"
                    ),
                    "packs": [level["label"].lower() for level in product.pack_levels] if product.has_packaging else [],
                    "stock": product.stock_display,
                    "out": bool(tracks_stock(product) and product.stock <= 0),
                    "low": bool(
                        tracks_stock(product) and product.stock > 0 and product.stock <= (product.low_stock_threshold or 0)
                    ),
                    "category": product.category_id or 0,
                    "find": " ".join(
                        str(part).lower()
                        for part in (
                            product.display_name,
                            product.variant_label,
                            product.sku,
                            product.barcode,
                            product.brand.name if product.brand_id else "",
                            product.category.name if product.category_id else "",
                            product.location,
                        )
                        if part
                    ),
                }
                for product in products
            ]
        }
    )


@any_module_access_required(("pos", "parties"), minimum=ACCESS_VIEW, write_level=ACCESS_VIEW)
@feature_required("parties")
@require_POST
def till_customer_add(request):
    """The till's side panel: a new customer without leaving the bill. Same form as the add-customer page."""
    form = CustomerForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"ok": False, "errors": {name: [str(error) for error in errors] for name, errors in form.errors.items()}},
            status=400,
        )
    customer = form.save()
    return JsonResponse({"ok": True, "customer": {"id": customer.id, "name": customer.name}})


@any_module_access_required(("pos", "parties"), minimum=ACCESS_VIEW, write_level=ACCESS_VIEW)
@feature_required("parties")
def add_customer_from_pos(request):
    next_url = get_safe_next_url(request, "customer_list")
    return_to_pos = next_url.startswith(reverse("pos_screen"))

    form = CustomerForm(request.POST or None)
    opening_form = _party_opening_form(request, "customer")
    if request.method == "POST":
        customer = None
        if form.is_valid() and (opening_form is None or opening_form.is_valid()):
            try:
                # The customer and the amount they already owe are saved together or not at all.
                with transaction.atomic():
                    customer = form.save()
                    _save_party_opening(request, customer, "customer", opening_form)
            except ValueError as exc:
                customer = None
                opening_form.add_error(None, str(exc))

        if customer is not None:
            messages.success(request, f"Customer '{customer.name}' added.")

            if return_to_pos:
                remember_customer_for_pos(request, customer.id, credit="credit=1" in next_url)
                separator = "&" if "?" in next_url else "?"
                return redirect(f"{next_url}{separator}customer_id={customer.id}")

            if request.POST.get("next") or request.GET.get("next"):
                return redirect(next_url)
            return redirect("customer_detail", id=customer.id)

        messages.error(request, "Please fix the customer details below.")

    return render(
        request,
        "pos/add_customer.html",
        {
            "form": form,
            "opening_form": opening_form,
            "next_url": next_url,
            "return_to_pos": return_to_pos,
        },
    )


@any_module_access_required(("pos", "reports"), minimum=ACCESS_VIEW)
def invoice(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("customer", "sold_by", "bank_account"), id=sale_id)
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only open receipts for their own sales.")
        return redirect("pos_screen")

    items = list(sale.items.select_related("product", "product__category"))
    subtotal = sum((item.line_total for item in items), Decimal("0"))
    line_discount_total = sum((item.line_discount_amount for item in items), Decimal("0"))
    bill_discount = bill_discounts.sale_bill_discount(sale, subtotal)
    discount_amount = bill_discount.amount
    tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
    settings_obj = get_system_settings()
    tax_label_value = sale_tax_label(sale, settings_obj.tax_label)
    company = get_active_company_profile()
    payment_method_totals = payment_method_totals_for_sale(sale, subtotal=subtotal, items=items)
    customer_badge = get_customer_badge(sale.customer) if sale.customer_id else None
    previous_balance = None
    new_balance = None
    if customer_badge:
        new_balance = quantize_money(customer_badge["amount"])
        previous_balance = quantize_money(new_balance - Decimal(sale.balance_due))

    context = {
        "sale": sale,
        "items": items,
        "subtotal": subtotal,
        "line_discount_total": line_discount_total,
        "discount_amount": discount_amount,
        "bill_discount": bill_discount,
        "tax_amount": tax_amount,
        "tax_label_value": tax_label_value,
        "grand_total": quantize_money(sale.total_amount or 0),
        "paid_amount": quantize_money(sale.paid_amount or 0),
        "balance_due": quantize_money(sale.balance_due),
        "company": company,
        "thermal_width_mm": settings_obj.thermal_printer_width_mm,
        "payment_method_totals": payment_method_totals,
        "show_payment_method_totals": bool(payment_method_totals),
        "previous_balance": previous_balance,
        "new_balance": new_balance,
        # The retail price (MRP) column shows when the price lists module is on and a product has one.
        "show_mrp": module_enabled("price_lists") and any(item.product.retail_price is not None for item in items),
    }
    # "Did not pay": a manager can put a paid sale (or part of it) on the customer's account.
    can_put_on_credit = (
        system_settings_allows_credit(settings_obj)
        and user_can_access_module(request.user, "pos", ACCESS_MANAGE)
        and not can_change_to_credit(sale)
    )
    context["can_put_on_credit"] = can_put_on_credit
    if can_put_on_credit and not sale.customer_id:
        context["credit_customers"] = Customer.objects.filter(is_active=True).order_by("name")
    return render(request, "pos/invoice.html", context)


def system_settings_allows_credit(settings_obj):
    return bool(settings_obj.allow_credit_sales) and module_enabled("parties")


@module_access_required("pos", minimum=ACCESS_MANAGE)
@require_POST
def sale_change_to_credit(request, sale_id):
    """The invoice page's "Put on credit": what the shop did not pay goes on its account."""
    sale = get_object_or_404(Sale, id=sale_id)
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only change their own sales.")
        return redirect("pos_screen")
    if not system_settings_allows_credit(get_system_settings()):
        messages.error(request, "Credit sales are switched off in Settings.")
        return redirect("invoice", sale_id=sale.id)
    customer = None
    customer_id = (request.POST.get("customer_id") or "").strip()
    if customer_id.isdigit():
        customer = Customer.objects.filter(pk=int(customer_id), is_active=True).first()
    try:
        warnings = change_sale_to_credit(
            sale=sale,
            received=request.POST.get("received") or "0",
            customer=customer,
            reason=request.POST.get("reason"),
            actor=request.user,
        )
    except (PaymentChangeRefused, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect(f"{reverse('invoice', kwargs={'sale_id': sale.id})}#put-on-credit")
    for warning in warnings:
        messages.warning(request, warning)
    sale.refresh_from_db()
    messages.success(
        request,
        f"Sale {sale.display_number} is now on credit: {format_money(sale.balance_due)} added to {sale.customer.name}'s account.",
    )
    return redirect("invoice", sale_id=sale.id)


def _pdf_currency_label(settings_obj):
    """The receipt PDF uses a built-in Latin-1 font, so a symbol it cannot draw falls back to the ISO code."""
    symbol = settings_obj.currency_symbol or ""
    try:
        symbol.encode("cp1252")
    except UnicodeEncodeError:
        return settings_obj.currency_code or ""
    return symbol


def _receipt_quantity(item):
    unit = item.unit_label
    if not unit or unit == Product.UNIT_PIECE:
        return format_qty(item.quantity)
    return f"{format_qty(item.quantity)} {unit}"


@any_module_access_required(("pos", "reports"), minimum=ACCESS_VIEW)
def invoice_pdf(request, sale_id):
    sale = get_object_or_404(Sale.objects.select_related("customer", "sold_by"), id=sale_id)
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only open receipts for their own sales.")
        return redirect("pos_screen")
    items = list(sale.items.select_related("product"))

    subtotal = sum((item.line_total for item in items), Decimal("0"))
    bill_discount = bill_discounts.sale_bill_discount(sale, subtotal)
    tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
    settings_obj = get_system_settings()
    tax_label_value = sale_tax_label(sale, settings_obj.tax_label)
    grand_total = Decimal(sale.total_amount or 0)
    printer_width_mm = settings_obj.thermal_printer_width_mm
    symbol = _pdf_currency_label(settings_obj)

    def money(value):
        return f"{symbol} {format_amount(value)}".strip()

    payment_method_totals = payment_method_totals_for_sale(sale, subtotal=subtotal, items=items)
    show_payment_method_totals = bool(payment_method_totals)
    cashier_name = sale.sold_by.get_full_name() or sale.sold_by.username if sale.sold_by_id else ""

    paper_width = printer_width_mm * mm
    left_margin = 4 * mm
    right_margin = paper_width - (4 * mm)
    line_height = 9
    total_lines = 17
    if sale.sold_by_id:
        total_lines += 1
    for item in items:
        total_lines += 2
        # A changed price, a scheme and a discount each get their own line under the item.
        total_lines += len(bill_discounts.receipt_item_notes(item, money))
    if sale.discount_rate > 0:
        total_lines += 1
    if tax_amount > 0:
        total_lines += 1
    if show_payment_method_totals:
        total_lines += len(payment_method_totals) * 2
    if sale.is_credit and sale.customer:
        total_lines += 3

    paper_height = (total_lines * line_height) + (14 * mm)
    y = paper_height - (6 * mm)

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="receipt_{sale.id}.pdf"'
    pdf = canvas.Canvas(response, pagesize=(paper_width, paper_height))

    company = get_active_company_profile()
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(paper_width / 2, y, (company.name if company else default_brand_name())[:24])
    y -= line_height

    if company and company.address:
        pdf.setFont("Helvetica", 6.5)
        pdf.drawCentredString(paper_width / 2, y, company.address[:34])
        y -= line_height

    if company and company.tax_registration_number:
        pdf.setFont("Helvetica", 6.5)
        pdf.drawCentredString(paper_width / 2, y, company.tax_registration_number[:34])
        y -= line_height

    pdf.setFont("Helvetica", 7)
    pdf.drawCentredString(paper_width / 2, y, "Sales Receipt")
    y -= line_height
    pdf.line(left_margin, y, right_margin, y)
    y -= line_height

    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(left_margin, y, f"Inv {sale.display_number}")
    pdf.drawRightString(right_margin, y, sale.created_at.strftime("%d-%m-%y"))
    y -= line_height
    pdf.drawString(left_margin, y, sale.created_at.strftime("%I:%M %p"))
    pdf.drawRightString(right_margin, y, sale.get_payment_method_display())
    y -= line_height
    pdf.drawString(left_margin, y, f"Cust: {(sale.customer.name if sale.customer else 'Walk-in')[:20]}")
    y -= line_height
    if sale.sold_by_id:
        pdf.drawString(left_margin, y, f"Cashier: {cashier_name[:18]}")
        y -= line_height
    if sale.is_credit:
        pdf.drawString(left_margin, y, "Type: Credit sale")
        y -= line_height
    pdf.line(left_margin, y, right_margin, y)
    y -= line_height

    for item in items:
        pdf.setFont("Helvetica-Bold", 7)
        pdf.drawString(left_margin, y, item.product.display_name[:24])
        y -= line_height

        # A discounted line reads: quantity x rate = gross, then "Disc 10% = Rs 20.00" and the net amount.
        has_line_discount = item.discount_percent > 0 and not getattr(item, "is_free", False)
        pdf.setFont("Helvetica", 6.8)
        rate = item.price if has_line_discount else item.discounted_unit_price
        pdf.drawString(left_margin, y, f"{_receipt_quantity(item)} x {money(rate)}")
        pdf.drawRightString(right_margin, y, money(item.base_line_total if has_line_discount else item.line_total))
        y -= line_height

        notes = bill_discounts.receipt_item_notes(item, money)
        if notes:
            pdf.setFont("Helvetica", 6.2)
            for note in notes:
                pdf.drawString(left_margin, y, note[:34])
                if has_line_discount and note.startswith("Disc "):
                    pdf.drawRightString(right_margin, y, money(item.line_total))
                y -= line_height

    pdf.line(left_margin, y, right_margin, y)
    y -= line_height

    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(left_margin, y, "Subtotal")
    pdf.drawRightString(right_margin, y, money(subtotal))
    y -= line_height

    if sale.discount_rate > 0:
        discount_left, discount_right = bill_discount.parts(money)
        pdf.drawString(left_margin, y, discount_left)
        pdf.drawRightString(right_margin, y, discount_right)
        y -= line_height

    if tax_amount > 0:
        label = tax_label_value[:10]
        if sale.tax_mode == Sale.TAX_MODE_MANUAL and Decimal(sale.tax_rate or 0) > 0:
            label = f"{label} {sale.tax_rate}%"[:18]
        pdf.drawString(left_margin, y, label)
        pdf.drawRightString(right_margin, y, money(tax_amount))
        y -= line_height

    if show_payment_method_totals:
        for option in payment_method_totals:
            pdf.drawString(left_margin, y, option["tax_label_display"][:24])
            pdf.drawRightString(right_margin, y, money(option["tax_amount"]))
            y -= line_height
            pdf.drawString(left_margin, y, f"{option['label']} total")
            pdf.drawRightString(right_margin, y, money(option["total_amount"]))
            y -= line_height

    # The total is ruled above and double-ruled below, like the printed slip.
    y -= 2
    pdf.line(left_margin, y + 7.8, right_margin, y + 7.8)
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawString(left_margin, y, "TOTAL")
    pdf.drawRightString(right_margin, y, money(grand_total))
    pdf.line(left_margin, y - 3, right_margin, y - 3)
    pdf.line(left_margin, y - 4.6, right_margin, y - 4.6)
    y -= line_height + 5

    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(left_margin, y, "Paid")
    pdf.drawRightString(right_margin, y, money(sale.paid_amount))
    y -= line_height
    pdf.drawString(left_margin, y, "Balance")
    pdf.drawRightString(right_margin, y, money(sale.balance_due))
    y -= line_height

    if sale.is_credit and sale.customer:
        pdf.line(left_margin, y, right_margin, y)
        y -= line_height
        current_balance = Decimal(sale.customer.balance or 0)
        previous_balance = quantize_money(current_balance - sale.balance_due)
        pdf.drawString(left_margin, y, "Prev balance")
        pdf.drawRightString(right_margin, y, money(previous_balance))
        y -= line_height
        pdf.drawString(left_margin, y, "Current balance")
        pdf.drawRightString(right_margin, y, money(current_balance))
        y -= line_height

    pdf.setFont("Helvetica", 6.2)
    footer_text = (company.invoice_footer if company and company.invoice_footer else "Thank you for your business.")[:40]
    pdf.drawCentredString(paper_width / 2, y, footer_text)
    pdf.showPage()
    pdf.save()
    return response


@module_access_required("pos", minimum=ACCESS_MANAGE)
@feature_required("returns")
def sale_return_view(request, sale_id):
    sale = get_object_or_404(
        Sale.objects.select_related("customer", "sold_by").prefetch_related("items__product__category", "items__return_items", "returns__items__product"),
        id=sale_id,
    )
    if not user_can_access_sale(request.user, sale):
        messages.error(request, "Cashiers can only return items from their own sales.")
        return redirect("pos_screen")

    initial = {"refund_method": SaleReturn.RECEIVABLE if sale.is_credit and sale.customer else SaleReturn.CASH}
    form = SaleReturnForm(request.POST or None, request.FILES or None, initial=initial)

    item_rows = [return_row(item) for item in sale.items.all()]

    if request.method == "POST" and form.is_valid():
        quantities = {row["item"].id: request.POST.get(f"qty_{row['item'].id}", "0") for row in item_rows}
        units = {row["item"].id: request.POST.get(f"unit_{row['item'].id}", "") for row in item_rows}
        try:
            sale_return = create_sale_return(
                sale=sale,
                quantities=quantities,
                units=units,
                refund_method=form.cleaned_data["refund_method"],
                bank_account=form.cleaned_data.get("bank_account"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
            attach_optional_receipt(request, sale_return, form.cleaned_data.get("bank_receipt"))
            messages.success(request, f"Sale return #{sale_return.id} recorded.")
            return redirect("invoice", sale_id=sale.id)
        except (ValueError, ValidationError) as exc:
            form.add_error(None, " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            messages.error(request, "Please review the sale return details.")

    existing_returns = sale.returns.prefetch_related("items__product").order_by("-created_at", "-id")
    return render(
        request,
        "pos/sale_return.html",
        {
            "sale": sale,
            "form": form,
            "item_rows": item_rows,
            "existing_returns": existing_returns,
        },
    )
