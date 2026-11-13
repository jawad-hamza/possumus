"""The customer ledger laid out as a statement.

Columns: Date | Reference / description | Qty | Debit | Credit | Balance, under a header that
names the customer, their city, route and order booker and the period. The balance is the
signed running balance: positive means the customer owes, negative means they have paid in
advance. Nothing is recalculated here: the entries, the opening balance and the closing
balance are the party ledger's own (accounts.services.ledgers), so the closing balance is
always the balance the rest of the app shows.

The quantity of an entry comes from pos.services.invoice_quantity, the one place that knows
how the lines of an invoice add up.
"""
from decimal import Decimal

from accounts.models import Voucher
from accounts.services.ledgers import ledger_rows
from accounts.services.party_statement import _document_labels
from accounts.services.report_utils import ZERO, csv_response, plain_amount, to_money

ZERO_QTY = Decimal("0")


def _quantities(vouchers):
    # Imported late: pos.services pulls in pos.models, which this app must not need at import time.
    from pos.services.invoice_quantity import voucher_quantities

    return voucher_quantities(vouchers)


def quantity_label():
    from pos.services.invoice_quantity import QTY_LABEL

    return QTY_LABEL


def customer_header(customer):
    """Who the statement is for: name, city, and (while routes and order bookers are on) those two."""
    from pos.services.field_sales import field_sales_enabled

    header = {"name": customer.name, "city": customer.city, "route": "", "order_booker": "", "phone": customer.phone}
    if field_sales_enabled():
        header["route"] = customer.route.name if customer.route_id else ""
        header["order_booker"] = customer.order_booker.name if customer.order_booker_id else ""
    return header


def statement_rows(entries, opening):
    """Rows of a page (or of the whole ledger) with reference, quantity and signed balance."""
    rows = ledger_rows(entries, opening)
    vouchers = [row["voucher"] for row in rows]
    references = _document_labels(vouchers)
    quantities = _quantities(vouchers)
    for row in rows:
        voucher = row["voucher"]
        row["reference"] = references.get(voucher.id) or f"{voucher.get_voucher_type_display()} #{voucher.id}"
        row["quantity"] = quantities.get(voucher.id)
        row["signed_balance"] = row["balance"] if row["drcr"] == "Dr" else -row["balance"]
    return rows


def period_quantity(entries):
    """Total quantity of every entry in the period (a queryset of ledger entries), returns taken off."""
    vouchers = Voucher.objects.filter(
        pk__in=entries.order_by().values("voucher_id"), ref_model__in=("Sale", "SaleReturn")
    ).only("id", "ref_model", "ref_id")
    return sum(_quantities(vouchers).values(), ZERO_QTY)


def statement_context(customer, data, rows):
    """Extra template context of the printable customer statement. ``data`` is get_party_ledger()'s result."""
    return {
        "customer_statement": True,
        "statement_header": customer_header(customer),
        "statement_rows": rows,
        "quantity_label": quantity_label(),
        "total_quantity": period_quantity(data["entries"]),
        "signed_opening": to_money(data["opening"]),
        "signed_closing": to_money(data["closing"]),
    }


def _plain_quantity(value):
    from pos.models import format_qty

    return "" if value is None else format_qty(value)


def statement_csv(customer, data, period_label, filename):
    """The whole statement of the period as CSV: header lines, opening balance, entries, totals."""
    header = customer_header(customer)
    rows = statement_rows(data["entries"].iterator(chunk_size=500), data["opening"])
    total_quantity = sum((row["quantity"] for row in rows if row["quantity"] is not None), ZERO_QTY)

    lines = [
        ["", "Customer", header["name"], "", "", "", ""],
        ["", "City", header["city"], "", "", "", ""],
    ]
    if header["route"] or header["order_booker"]:
        lines.append(["", "Route", header["route"], "", "", "", ""])
        lines.append(["", "Order booker", header["order_booker"], "", "", "", ""])
    lines.append(["", "Period", period_label, "", "", "", ""])
    lines.append(["", "Opening balance", "", "", "", "", plain_amount(data["opening"])])
    for row in rows:
        lines.append(
            [
                row["date"].isoformat(),
                row["reference"],
                row["narration"] or "",
                _plain_quantity(row["quantity"]),
                plain_amount(row["debit"]) if row["debit"] else "",
                plain_amount(row["credit"]) if row["credit"] else "",
                plain_amount(row["signed_balance"]),
            ]
        )
    lines.append(
        [
            "",
            "Closing balance",
            "",
            _plain_quantity(total_quantity),
            plain_amount(data["total_debit"] or ZERO),
            plain_amount(data["total_credit"] or ZERO),
            plain_amount(data["closing"]),
        ]
    )
    return csv_response(
        filename,
        ["Date", "Reference", "Description", quantity_label(), "Debit", "Credit", "Balance"],
        lines,
    )
