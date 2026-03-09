from decimal import Decimal, InvalidOperation

from pos.models import CompanyProfile, Sale, SystemSettings, quantize_money


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


def get_active_company_profile(create=False):
    company = CompanyProfile.objects.filter(is_active=True).first()
    if company or not create:
        return company
    return CompanyProfile.objects.create(
        name="POS IMS",
        invoice_footer="Thank you for your business.",
        is_active=True,
    )


def get_system_settings():
    return SystemSettings.load()


def category_tax_rate_for_product(product):
    category = getattr(product, "category", None)
    if not category:
        return Decimal("0")
    return Decimal(category.effective_gst_rate or 0)


def sale_discount_amount(subtotal, discount_rate):
    subtotal = safe_decimal(subtotal)
    return quantize_money(subtotal * (normalized_rate(discount_rate) / Decimal("100")))


def sale_tax_label(sale, fallback="GST"):
    return (getattr(sale, "tax_label", "") or fallback or "GST").strip() or "GST"


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


def category_tax_from_rows(rows, discount_amount):
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
        tax_rate = normalized_rate(row.get("category_tax_rate", 0))
        total_tax += quantize_money(taxable_base * (tax_rate / Decimal("100")))

    return quantize_money(total_tax)


def sale_totals_from_rows(rows, discount_rate, manual_tax_rate, settings_obj):
    subtotal = sum((Decimal(row["line_total"]) for row in rows), Decimal("0"))
    line_discount_total = sum((Decimal(row.get("line_discount_amount", 0)) for row in rows), Decimal("0"))
    discount_amount = sale_discount_amount(subtotal, discount_rate)
    after_discount = quantize_money(subtotal - discount_amount)

    if settings_obj.uses_category_gst:
        applied_tax_rate = Decimal("0")
        tax_amount = category_tax_from_rows(rows, discount_amount)
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
    }
