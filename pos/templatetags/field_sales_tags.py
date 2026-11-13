"""Template helpers for city, route, order booker and credit limit.

They let the customer pages (and the till's customer panel) get what they need without the
views that render them having to know about routes and order bookers.
"""
from django import template

from pos.services import field_sales
from pos.services.currency import format_amount
from pos.services.quick_add import quick_add_context

register = template.Library()


@register.filter
def statement_amount(value):
    """An amount on the customer statement: 12,500 rather than 12,500.00, as on the printed one.

    Paisa are never hidden: 12,500.50 keeps its decimals.
    """
    text = format_amount(value)
    return text[:-3] if text.endswith(".00") else text


@register.simple_tag(takes_context=True)
def field_sales_quick_add(context):
    """The "Add route" / "Add order booker" side-panel forms this user may use.

    Returns {"quick_add": [...], "quick_add_kinds": {...}} like pos.services.quick_add.
    """
    request = context.get("request")
    if request is None or not field_sales.field_sales_enabled():
        return {"quick_add": [], "quick_add_kinds": {}}
    return quick_add_context(request, "route", "order_booker")


@register.simple_tag
def route_defaults():
    """{route id: {"city", "booker"}}: what choosing a route fills in on a customer form."""
    if not field_sales.field_sales_enabled():
        return {}
    return field_sales.route_defaults()


@register.simple_tag
def till_order_bookers():
    """Order bookers the till may record a sale against; empty while the module is off."""
    if not field_sales.field_sales_enabled():
        return []
    return list(field_sales.active_order_bookers())


@register.simple_tag
def customer_credit(customer):
    """Where the customer stands against their credit limit, for the customer page."""
    standing = field_sales.credit_standing(customer)
    standing["note"] = field_sales.credit_limit_note(standing)
    return standing
