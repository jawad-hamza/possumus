from django import template

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
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return "Rs 0.00"
    return f"Rs {amount:,.2f}"
