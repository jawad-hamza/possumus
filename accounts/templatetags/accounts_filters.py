# accounts/templatetags/accounts_filters.py
from django import template

register = template.Library()

@register.filter
def absval(v):
    try:
        return abs(v)
    except Exception:
        return v

@register.filter
def drcr(balance, normal="dr"):
    """
    Returns "Dr" or "Cr" label based on sign and the normal side.
    normal="dr": positive => Dr, negative => Cr
    normal="cr": positive => Cr, negative => Dr
    """
    try:
        b = float(balance)
    except Exception:
        return ""

    if normal == "cr":
        return "Cr" if b >= 0 else "Dr"
    return "Dr" if b >= 0 else "Cr"


@register.filter
def badge_class_for_sign(balance):
    """
    Positive => success, Negative => danger, Zero => secondary
    """
    try:
        b = float(balance)
    except Exception:
        return "secondary"
    if b > 0:
        return "success"
    if b < 0:
        return "danger"
    return "secondary"
