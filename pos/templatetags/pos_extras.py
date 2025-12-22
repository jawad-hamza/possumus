from django import template
from decimal import Decimal

register = template.Library()

@register.filter
def get_item(d, key):
    try:
        return d.get(str(key), 0)
    except Exception:
        return 0

@register.filter
def multiply(a, b):
    try:
        return Decimal(a) * Decimal(b)
    except Exception:
        return 0