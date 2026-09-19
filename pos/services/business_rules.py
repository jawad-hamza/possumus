from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from pos.models import CompanyProfile, Product, Sale, SystemSettings, quantize_money


def safe_decimal(value):
    try:
        return Decimal(value)
    except (TypeError, InvalidOperation):
        return Decimal("0")


def normalized_rate(value):
    rate = safe_decimal(value)
    if rate < 0:
        return Decimal("0")
    if rate > 100:
        return Decimal("100")
    return quantize_money(rate)


def default_brand_name():
    return getattr(settings, "PUBLIC_BRAND_NAME", "") or "ZehnMS"


def get_active_company_profile(create=False):
    company = CompanyProfile.objects.filter(is_active=True).first()
    if company or not create:
        return company
    return CompanyProfile.objects.create(
        name=default_brand_name(),
        invoice_footer="Thank you for your business.",
        is_active=True,
    )


def get_system_settings():
    return SystemSettings.load()


def category_tax_rate_for_product(product, payment_method=Sale.CASH):
    category = getattr(product, "category", None)
    if not category:
        return Decimal("0")
    return Decimal(category.gst_rate_for_payment_method(payment_method) or 0)


def line_tax_rate_for_item(item, payment_method):
    if hasattr(item, "tax_rate_for_payment_method"):
        return Decimal(item.tax_rate_for_payment_method(payment_method) or 0)
    product = getattr(item, "product", None)
    return category_tax_rate_for_product(product, payment_method)


def sale_discount_amount(subtotal, discount_rate):
    subtotal = safe_decimal(subtotal)
    return quantize_money(subtotal * (normalized_rate(discount_rate) / Decimal("100")))


def sale_tax_label(sale, fallback="GST"):
    return (getattr(sale, "tax_label", "") or fallback or "GST").strip() or "GST"


def payment_method_tax_rate(settings_obj, payment_method):
    if not getattr(settings_obj, "uses_payment_method_gst", False):
        return Decimal("0")
    return normalized_rate(settings_obj.gst_rate_for_payment_method(payment_method))


def payment_method_totals_from_rates(*, subtotal, discount_rate, cash_rate, bank_rate, tax_label="GST"):
    subtotal = safe_decimal(subtotal)
    discount_amount = sale_discount_amount(subtotal, discount_rate)
    after_discount = quantize_money(subtotal - discount_amount)

    options = []
    for method, label, rate in (
        (Sale.CASH, "Cash", normalized_rate(cash_rate)),
        (Sale.BANK, "Bank", normalized_rate(bank_rate)),
    ):
        tax_amount = quantize_money(after_discount * (rate / Decimal("100")))
        options.append(
            {
                "payment_method": method,
                "label": label,
                "rate": rate,
                "tax_amount": tax_amount,
                "total_amount": quantize_money(after_discount + tax_amount),
                "tax_label_display": f"{label} {tax_label} {rate:.2f}%",
            }
        )
    return options


def payment_method_totals_for_settings(*, subtotal, discount_rate, settings_obj):
    return payment_method_totals_from_rates(
        subtotal=subtotal,
        discount_rate=discount_rate,
        cash_rate=settings_obj.gst_rate_for_payment_method(Sale.CASH),
        bank_rate=settings_obj.gst_rate_for_payment_method(Sale.BANK),
        tax_label=settings_obj.tax_label,
    )


def category_tax_from_rows(rows, discount_amount, payment_method=Sale.CASH):
    subtotal = sum((Decimal(row["line_total"]) for row in rows), Decimal("0"))
    if subtotal <= 0:
        return Decimal("0")

    remaining_discount = quantize_money(discount_amount)
    total_tax = Decimal("0")

    for index, row in enumerate(rows):
        line_total = Decimal(row["line_total"])
        if index == len(rows) - 1:
            allocated_discount = remaining_discount
        else:
            share = line_total / subtotal
            allocated_discount = quantize_money(Decimal(discount_amount) * share)
            remaining_discount -= allocated_discount

        taxable_base = max(line_total - Decimal(allocated_discount), Decimal("0"))
        if payment_method == Sale.BANK:
            tax_rate = normalized_rate(row.get("bank_category_tax_rate", row.get("category_tax_rate", 0)))
        else:
            tax_rate = normalized_rate(row.get("cash_category_tax_rate", row.get("category_tax_rate", 0)))
        total_tax += quantize_money(taxable_base * (tax_rate / Decimal("100")))

    return quantize_money(total_tax)


def category_payment_method_totals_from_rows(rows, discount_rate, tax_label="GST"):
    subtotal = sum((Decimal(row["line_total"]) for row in rows), Decimal("0"))
    discount_amount = sale_discount_amount(subtotal, discount_rate)
    after_discount = quantize_money(subtotal - discount_amount)

    options = []
    for method, label in ((Sale.CASH, "Cash"), (Sale.BANK, "Bank")):
        tax_amount = category_tax_from_rows(rows, discount_amount, payment_method=method)
        options.append(
            {
                "payment_method": method,
                "label": label,
                "rate": None,
                "tax_amount": tax_amount,
                "total_amount": quantize_money(after_discount + tax_amount),
                "tax_label_display": f"{label} {tax_label}",
            }
        )
    return options


def comparison_totals_if_needed(options):
    if len(options) < 2:
        return []
    left, right = options[0], options[1]
    same_total = Decimal(left["total_amount"]) == Decimal(right["total_amount"])
    same_tax = Decimal(left["tax_amount"]) == Decimal(right["tax_amount"])
    same_rate = left.get("rate") == right.get("rate")
    if same_total and same_tax and same_rate:
        return []
    if all(Decimal(option["tax_amount"]) <= 0 for option in options) and same_total:
        return []
    return options


def payment_method_totals_for_sale(sale, *, subtotal=None, items=None):
    if subtotal is None:
        if items is None:
            items = sale.items.all()
        subtotal = sum((item.line_total for item in items), Decimal("0"))

    if sale.tax_mode == Sale.TAX_MODE_CATEGORY:
        if items is None:
            items = sale.items.select_related("product__category")
        rows = [
            {
                "line_total": item.line_total,
                "cash_category_tax_rate": line_tax_rate_for_item(item, Sale.CASH),
                "bank_category_tax_rate": line_tax_rate_for_item(item, Sale.BANK),
                "category_tax_rate": line_tax_rate_for_item(item, sale.payment_method),
            }
            for item in items
        ]
        return comparison_totals_if_needed(
            category_payment_method_totals_from_rows(rows, getattr(sale, "discount_rate", 0), sale_tax_label(sale))
        )

    cash_rate = getattr(sale, "cash_tax_rate_snapshot", 0) or getattr(sale, "tax_rate", 0)
    bank_rate = getattr(sale, "bank_tax_rate_snapshot", 0) or getattr(sale, "tax_rate", 0)
    return comparison_totals_if_needed(
        payment_method_totals_from_rates(
            subtotal=subtotal,
            discount_rate=getattr(sale, "discount_rate", 0),
            cash_rate=cash_rate,
            bank_rate=bank_rate,
            tax_label=sale_tax_label(sale),
        )
    )


def sale_tax_amount(sale, subtotal=None, items=None):
    stored_tax = quantize_money(getattr(sale, "tax_amount", 0) or 0)
    manual_tax_rate = normalized_rate(getattr(sale, "tax_rate", 0) or 0)

    if stored_tax > 0 or manual_tax_rate <= 0:
        return stored_tax

    if subtotal is None:
        if items is None:
            items = sale.items.all()
        subtotal = sum((item.line_total for item in items), Decimal("0"))

    discount_amount = sale_discount_amount(subtotal, getattr(sale, "discount_rate", 0) or 0)
    after_discount = Decimal(subtotal) - Decimal(discount_amount)
    return quantize_money(after_discount * (manual_tax_rate / Decimal("100")))


def sale_totals_from_rows(rows, discount_rate, manual_tax_rate, settings_obj, payment_method=Sale.CASH):
    subtotal = sum((Decimal(row["line_total"]) for row in rows), Decimal("0"))
    line_discount_total = sum((Decimal(row.get("line_discount_amount", 0)) for row in rows), Decimal("0"))
    discount_amount = sale_discount_amount(subtotal, discount_rate)
    after_discount = quantize_money(subtotal - discount_amount)

    comparison_totals = []
    if settings_obj.uses_category_gst:
        applied_tax_rate = Decimal("0")
        tax_amount = category_tax_from_rows(rows, discount_amount, payment_method=payment_method)
        comparison_totals = comparison_totals_if_needed(
            category_payment_method_totals_from_rows(rows, discount_rate, settings_obj.tax_label)
        )
    elif settings_obj.uses_payment_method_gst:
        applied_tax_rate = payment_method_tax_rate(settings_obj, payment_method)
        tax_amount = quantize_money(after_discount * (applied_tax_rate / Decimal("100")))
        comparison_totals = comparison_totals_if_needed(
            payment_method_totals_for_settings(
                subtotal=subtotal,
                discount_rate=discount_rate,
                settings_obj=settings_obj,
            )
        )
    elif settings_obj.allows_manual_tax:
        applied_tax_rate = normalized_rate(manual_tax_rate)
        tax_amount = quantize_money(after_discount * (applied_tax_rate / Decimal("100")))
    else:
        applied_tax_rate = Decimal("0")
        tax_amount = Decimal("0")

    total_amount = quantize_money(after_discount + tax_amount)

    return {
        "subtotal": quantize_money(subtotal),
        "line_discount_total": quantize_money(line_discount_total),
        "discount_amount": quantize_money(discount_amount),
        "net_sales": quantize_money(after_discount),
        "tax_amount": quantize_money(tax_amount),
        "total_amount": total_amount,
        "applied_tax_rate": applied_tax_rate,
        "comparison_totals": comparison_totals,
    }


def low_stock_products():
    """Active products at or below their reorder level, the emptiest first."""
    return (
        Product.objects.filter(stock__lte=F("low_stock_threshold"), is_active=True, track_stock=True)
        .select_related("category")
        .order_by("stock", "name")
    )


def near_expiry_products(settings_obj, *, today=None):
    if not getattr(settings_obj, "enable_product_expiry", False):
        return Product.objects.none()
    today = today or timezone.localdate()
    alert_end = today + timedelta(days=int(settings_obj.expiry_alert_days or 30))
    return Product.objects.filter(
        is_active=True,
        expiry_date__isnull=False,
        expiry_date__gte=today,
        expiry_date__lte=alert_end,
    ).select_related("category", "brand", "supplier").order_by("expiry_date", "name")


def expired_products(settings_obj, *, today=None):
    if not getattr(settings_obj, "enable_product_expiry", False):
        return Product.objects.none()
    today = today or timezone.localdate()
    return Product.objects.filter(
        is_active=True,
        expiry_date__isnull=False,
        expiry_date__lt=today,
    ).select_related("category", "brand", "supplier").order_by("expiry_date", "name")
