"""The one way a sale is written: the sale, its lines, the checks a sale must pass and its voucher.

The till calls this through ``pos.views.pos_views._record_sale``; the distribution app calls it
when a booked order becomes an invoice. Both get the same rules: price override and line
discount as Settings allow, the minimum margin, the bill discount, zero-total sales, the
credit limit (refuse or warn) and invoice numbering, then the ledger posting.

Callers wrap the call in a savepoint: a refusal raises ``SaleRefused`` and the half-written
sale, its stock movement and its voucher lines roll back together.
"""
from decimal import Decimal

from django.utils import timezone

from accounts.services.pos_posting import post_sale_from_pos
from pos.models import PACK_PIECE, Sale, SaleItem, quantize_money
from pos.services import discounts as bill_discounts
from pos.services.cart_pricing import to_decimal, validate_cart_line
from pos.services.field_sales import credit_limit_problem, credit_limit_refuses


class SaleRefused(Exception):
    """A sale that must not be saved, with a reason the person at the screen can read."""


def _known_schemes(rows):
    """Ids of the schemes the rows name that still exist (a scheme deleted meanwhile is dropped)."""
    ids = {str(row.get("scheme_id")) for row in rows if str(row.get("scheme_id") or "").isdigit()}
    if not ids:
        return set()
    from pricing.models import Scheme

    return {str(pk) for pk in Scheme.objects.filter(pk__in=ids).values_list("pk", flat=True)}


def _scheme_discount_given(item, by_scheme_price, by_scheme_discount):
    """The money a scheme took off a saved line: an amount off the rate, or its percentage."""
    given = Decimal("0")
    if by_scheme_price:
        given += quantize_money((Decimal(item.original_price) - Decimal(item.price)) * Decimal(item.quantity))
    if by_scheme_discount:
        given += item.line_discount_amount
    return quantize_money(max(given, Decimal("0")))


def record_sale(
    *,
    user,
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
    order_booker=None,
    warn=None,
    post=None,
):
    """Create the sale, its lines and its voucher. Returns (sale, settings_adjusted_lines).

    ``rows`` are bill lines: product_id, quantity, unit_level, unit_price, original_price,
    discount_percent and the cash / bank category tax rates. ``warn`` receives the sentence
    for a credit sale let through over the limit (Settings: warn only).
    """
    sale = Sale(
        customer=customer,
        sold_by=user,
        is_credit=is_credit,
        payment_method=payment_method,
        bank_account=bank_account,
        paid_amount=Decimal("0"),
        discount_rate=discount_rate,
        tax_rate=(
            system_settings.gst_rate_for_payment_method(payment_method)
            if system_settings.uses_payment_method_gst
            else tax_rate
        ),
        tax_mode=Sale.TAX_MODE_CATEGORY if system_settings.uses_category_gst else Sale.TAX_MODE_MANUAL,
        tax_label=system_settings.tax_label,
        cash_tax_rate_snapshot=system_settings.gst_rate_for_payment_method(Sale.CASH),
        bank_tax_rate_snapshot=system_settings.gst_rate_for_payment_method(Sale.BANK),
    )
    sale.order_booker = order_booker
    bill_discounts.apply_typed_discount(sale, typed_discount, discount_rate)
    sale.full_clean()
    sale.save()

    settings_adjusted_lines = False
    known_schemes = _known_schemes(rows)
    for row in rows:
        product = locked_products.get(int(row["product_id"]))
        level = row.get("unit_level") or PACK_PIECE
        scheme_id = row.get("scheme_id") if str(row.get("scheme_id") or "") in known_schemes else None
        if row.get("is_free"):
            # Free goods of a scheme: a line at zero that takes its cost out of stock.
            if Decimal(row["quantity"] or 0) <= 0:
                continue
            SaleItem.objects.create(
                sale=sale,
                product=product,
                quantity=row["quantity"],
                unit_level=level,
                original_price=Decimal("0"),
                price=Decimal("0"),
                discount_percent=Decimal("0"),
                cost_price=product.pack_cost_exact(level),
                is_free=True,
                scheme_id=scheme_id,
                scheme_name=(row.get("scheme_name") or "")[:100],
            )
            continue
        original_price = to_decimal(row["original_price"], product.pack_price(level))
        # A rate or discount a scheme set is not a price override or a line discount: Settings do not undo it.
        scheme_price = to_decimal(row.get("scheme_price"), None)
        scheme_discount = to_decimal(row.get("scheme_discount"), None)
        by_scheme_price = scheme_price is not None and row["unit_price"] == scheme_price
        by_scheme_discount = scheme_discount is not None and row["discount_percent"] == scheme_discount
        price = row["unit_price"] if system_settings.allow_price_override or by_scheme_price else original_price
        discount_percent = (
            row["discount_percent"] if system_settings.allow_line_discount or by_scheme_discount else Decimal("0")
        )
        validated = validate_cart_line(product, price, discount_percent, level)

        if (
            (not system_settings.allow_price_override and not by_scheme_price and row["unit_price"] != original_price)
            or (not system_settings.allow_line_discount and not by_scheme_discount and row["discount_percent"] > 0)
        ):
            settings_adjusted_lines = True

        item = SaleItem.objects.create(
            sale=sale,
            product=product,
            quantity=row["quantity"],
            unit_level=level,
            original_price=original_price,
            price=validated["price"],
            discount_percent=validated["discount_percent"],
            cost_price=product.pack_cost_exact(level),
            cash_tax_rate_snapshot=row.get("cash_category_tax_rate", Decimal("0")),
            bank_tax_rate_snapshot=row.get("bank_category_tax_rate", Decimal("0")),
        )
        given = _scheme_discount_given(item, by_scheme_price, by_scheme_discount)
        if scheme_id and given > 0:
            SaleItem.objects.filter(pk=item.pk).update(
                scheme_id=scheme_id, scheme_name=(row.get("scheme_name") or "")[:100], scheme_discount=given
            )

    sale.refresh_from_db()
    discount_refusal = bill_discounts.sale_discount_refusal(sale)
    if discount_refusal:
        # A cash discount larger than the lines it was given on is never trimmed to fit.
        raise SaleRefused(discount_refusal)
    zero_total = Decimal(sale.total_amount or 0) <= 0
    if zero_total and not system_settings.allow_zero_total_sales:
        # A 100% order discount (or a weight too small to price) is a giveaway; the owner must allow those.
        raise SaleRefused(
            "This sale adds up to zero. Lower the discount or check the quantities, "
            "or ask the owner to allow zero-total sales in Settings."
        )
    if zero_total and is_credit:
        raise SaleRefused("A zero-total sale has nothing to put on credit. Complete it as cash.")
    if is_credit:
        # The whole bill goes on the customer's account: it must fit inside their credit limit.
        over_limit = credit_limit_problem(customer, sale.total_amount, lock=True)
        if over_limit and credit_limit_refuses(system_settings):
            raise SaleRefused(
                f"Credit sale refused. {over_limit} Take a payment first, or raise the limit on the customer's page."
            )
        if over_limit and warn is not None:
            warn(f"Sold over the credit limit. {over_limit}")

    sale.paid_amount = Decimal("0") if is_credit else Decimal(sale.total_amount or 0)
    sale.bank_account = bank_account if payment_method == Sale.BANK and not is_credit else None
    sale.full_clean()
    sale.save(update_fields=["paid_amount", "bank_account"])

    (post or post_sale_from_pos)(
        sale_id=sale.id,
        customer=sale.customer,
        total_amount=sale.total_amount,
        paid_amount=sale.paid_amount,
        payment_method=sale.payment_method,
        bank_account=sale.bank_account,
        sale_date=timezone.localtime(sale.created_at).date(),
        tax_amount=sale.tax_amount,
        allow_zero_total=zero_total,
    )
    return sale, settings_adjusted_lines
