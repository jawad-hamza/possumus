"""The "Qty" figure of an invoice: how much goods one sale (or one sale return) carried.

``invoice_quantity`` is the ONLY place that says how the lines of a document add up to one
quantity. The customer ledger, its print view and CSV, and the sales by order booker report
all read it from here, so a change in what a line counts for (for example lines sold in
cartons or boxes that must be turned into pieces or kilograms) is made in this one function.

The other helpers only fetch the documents in bulk and hand each one to ``invoice_quantity``.
"""
from decimal import Decimal

from ..models import Sale, SaleReturn, quantize_qty

ZERO_QTY = Decimal("0")
QTY_LABEL = "Qty"


def _line_quantity(line):
    """What one SaleItem / SaleReturnItem adds to its invoice's quantity.

    SWITCH POINT for carton / box / piece packaging: once SaleItem and SaleReturnItem carry
    ``unit_factor`` (pieces per unit of the line) and ``quantity`` is in the line's own unit,
    this becomes ``Decimal(line.quantity or 0) * Decimal(line.unit_factor or 1)`` so the
    invoice adds up in base units. Nothing else in the ledger, statement or reports changes.

    Packaging is in: lines keep the unit they were sold in, so they are turned into pieces here.
    """
    return Decimal(line.quantity or 0) * Decimal(getattr(line, "unit_factor", 1) or 1)


def invoice_quantity(document):
    """Total quantity of goods on a sale invoice; negative for a sale return.

    Today every line counts for its own quantity, whatever its unit. ``document`` is a Sale or
    a SaleReturn; both keep their lines on ``items`` and each line has a ``quantity``.
    """
    total = sum((_line_quantity(line) for line in document.items.all()), ZERO_QTY)
    if isinstance(document, SaleReturn):
        total = -total
    return quantize_qty(total)


def invoice_quantities(sale_ids):
    """{sale id: quantity} for many invoices, in two queries."""
    sale_ids = {pk for pk in sale_ids if pk}
    if not sale_ids:
        return {}
    sales = Sale.objects.filter(pk__in=sale_ids).prefetch_related("items")
    return {sale.pk: invoice_quantity(sale) for sale in sales}


def return_quantities(sale_return_ids):
    """{sale return id: quantity, as a negative number} for many returns, in two queries."""
    sale_return_ids = {pk for pk in sale_return_ids if pk}
    if not sale_return_ids:
        return {}
    sale_returns = SaleReturn.objects.filter(pk__in=sale_return_ids).prefetch_related("items")
    return {sale_return.pk: invoice_quantity(sale_return) for sale_return in sale_returns}


def voucher_quantities(vouchers):
    """{voucher id: quantity} for the ledger entries that moved goods.

    A sale carries its invoice quantity, a sale return the quantity that came back (negative).
    Payments, opening balances and anything else are left out: they moved no goods.
    """
    vouchers = list(vouchers)
    sales = invoice_quantities(v.ref_id for v in vouchers if v.ref_model == "Sale")
    returns = return_quantities(v.ref_id for v in vouchers if v.ref_model == "SaleReturn")
    quantities = {}
    for voucher in vouchers:
        if voucher.ref_model == "Sale" and voucher.ref_id in sales:
            quantities[voucher.id] = sales[voucher.ref_id]
        elif voucher.ref_model == "SaleReturn" and voucher.ref_id in returns:
            quantities[voucher.id] = returns[voucher.ref_id]
    return quantities
