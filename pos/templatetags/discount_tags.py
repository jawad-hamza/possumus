from django import template

from pos.services import discounts

register = template.Library()


@register.filter
def discount_equals(percent, amount):
    """A discount percentage with what it comes to: "10% = Rs 20.00"."""
    return discounts.discount_equals(percent, amount)


@register.filter
def discount_cell(sale):
    """A sale's discount for a report column: "10% = 20.00" typed as a percentage, "20.00 (9.3%)" typed as cash."""
    return discounts.sale_bill_discount(sale, getattr(sale, "report_subtotal", None)).cell()
