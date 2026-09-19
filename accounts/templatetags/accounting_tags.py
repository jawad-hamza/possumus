from django import template

from pos.templatetags.currency_tags import money as format_with_currency

register = template.Library()


@register.filter
def drcr(amount):
    """
    Converts signed numbers into accounting format.
    -500 -> 500 Cr
     500 -> 500 Dr
    """
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return ""

    if amount < 0:
        return f"{abs(amount):,.2f} Cr"
    return f"{amount:,.2f} Dr"


@register.filter
def money(amount):
    # Same output as the pos currency_tags filter, so ledgers follow the shop's currency.
    return format_with_currency(amount)
