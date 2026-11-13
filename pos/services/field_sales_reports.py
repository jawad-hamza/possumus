"""Reports for a distribution business, built on the table-report framework of pos.services.reporting.

* Receivables by city, route or order booker: every customer with a balance, grouped under a
  "City : Mirpur A.K" line with a subtotal per group, like the statement the owner prints.
* Sales by order booker: invoices, quantity and sales each order booker brought in.

pos.services.reporting registers both and imports this module on first use (this module
imports that one, so it must not be imported while that one is still loading).
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from accounts.models import PartyLedgerEntry
from pos.models import Customer, OrderBooker, Sale, SaleReturn, format_qty, quantize_money
from pos.services.field_sales import field_sales_enabled
from pos.services.invoice_quantity import QTY_LABEL, invoice_quantity
from pos.services.reporting import (
    INT,
    MONEY,
    ZERO,
    Column,
    Report,
    _card,
    _money_sum,
    _sale_due_expression,
    _select_filter,
    range_q,
)

ZERO_QTY = Decimal("0")

GROUP_CITY = "city"
GROUP_ROUTE = "route"
GROUP_BOOKER = "order_booker"
GROUP_LABELS = {GROUP_CITY: "City", GROUP_ROUTE: "Route", GROUP_BOOKER: "Order booker"}
GROUP_EMPTY = {GROUP_CITY: "No city", GROUP_ROUTE: "No route", GROUP_BOOKER: "No order booker"}

SHOW_BALANCES = ""
SHOW_OWING = "owing"
SHOW_EVERYONE = "everyone"
SHOW_OPTIONS = (
    (SHOW_BALANCES, "Customers with a balance"),
    (SHOW_OWING, "Only customers who owe"),
    (SHOW_EVERYONE, "Every customer"),
)


def customer_balances(as_of=None):
    """{customer id: ledger balance} from the party sub-ledger, the same sum the customer page shows.

    ``as_of`` limits it to entries dated up to that day. Today or later means everything, so the
    report always agrees with the balances on screen.
    """
    entries = PartyLedgerEntry.objects.filter(content_type=ContentType.objects.get_for_model(Customer))
    if as_of is not None and as_of < timezone.localdate():
        entries = entries.filter(voucher__date__lte=as_of)
    rows = (
        entries.values("object_id")
        .annotate(balance=_money_sum("debit") - _money_sum("credit"))
        .order_by()
        .values_list("object_id", "balance")
    )
    return {object_id: quantize_money(balance or 0) for object_id, balance in rows}


def _group_of(customer, group):
    if group == GROUP_ROUTE:
        return customer.route.label if customer.route_id else ""
    if group == GROUP_BOOKER:
        return customer.order_booker.name if customer.order_booker_id else ""
    return customer.city


def build_receivables_by_area(start, end, params):
    uses_field_sales = field_sales_enabled()
    group_options = [(GROUP_CITY, "City")]
    if uses_field_sales:
        group_options += [(GROUP_ROUTE, "Route"), (GROUP_BOOKER, "Order booker")]
    group = (params.get("group") or "").strip()
    if group not in dict(group_options):
        group = GROUP_CITY
    show = (params.get("show") or "").strip()
    if show not in dict(SHOW_OPTIONS):
        show = SHOW_BALANCES

    balances = customer_balances(end)
    customers = Customer.objects.select_related("route", "order_booker").order_by("name", "id")

    groups = {}
    for customer in customers:
        balance = balances.get(customer.id, ZERO)
        if show == SHOW_OWING and balance <= 0:
            continue
        if show == SHOW_BALANCES and balance == 0:
            continue
        groups.setdefault(_group_of(customer, group) or "", []).append(
            {
                "customer": customer.name,
                "customer_url": reverse("customer_detail", args=[customer.id]),
                "phone": customer.phone,
                "city": customer.city,
                "route": customer.route.name if customer.route_id else "",
                "order_booker": customer.order_booker.name if customer.order_booker_id else "",
                "credit_limit": customer.credit_limit,
                "balance": balance,
            }
        )

    label = GROUP_LABELS[group]
    rows = []
    customer_count = 0
    owed = advances = ZERO
    # Named groups A to Z, the customers with nothing recorded last.
    for name in sorted(groups, key=lambda value: (value == "", value.casefold())):
        members = groups[name]
        shown_name = name or GROUP_EMPTY[group]
        subtotal = quantize_money(sum((row["balance"] for row in members), ZERO))
        rows.append({"_kind": "group", "customer": f"{label} : {shown_name}"})
        rows.extend(members)
        rows.append(
            {
                "_kind": "subtotal",
                "customer": f"Total {shown_name}",
                "phone": f"{len(members)} customer{'s' if len(members) != 1 else ''}",
                "balance": subtotal,
            }
        )
        customer_count += len(members)
        owed += sum((row["balance"] for row in members if row["balance"] > 0), ZERO)
        advances += sum((-row["balance"] for row in members if row["balance"] < 0), ZERO)

    columns = [
        Column("customer", "Customer", url_key="customer_url"),
        Column("phone", "Phone"),
        Column("city", "City"),
    ]
    if uses_field_sales:
        columns += [Column("route", "Route"), Column("order_booker", "Order Booker")]
    columns += [Column("credit_limit", "Credit Limit", MONEY), Column("balance", "Balance", MONEY)]

    net = quantize_money(owed - advances)
    return Report(
        columns=columns,
        rows=rows,
        totals={"balance": net} if rows else {},
        cards=[
            _card("Customers", customer_count, INT, "slate", f"In {len(groups)} group{'s' if len(groups) != 1 else ''} by {label.lower()}"),
            _card("Customers Owe You", quantize_money(owed), accent="warning", meta="Balances still to collect"),
            _card("Advances You Hold", quantize_money(advances), accent="danger", meta="Paid before buying, shown as a minus"),
            _card("Net Receivable", net, accent="primary", meta="What is owed less the advances"),
        ],
        filters=[
            _select_filter("group", "Group by", group, group_options),
            _select_filter("show", "Show", show, SHOW_OPTIONS),
        ],
        note="A minus balance means the customer has paid in advance. Open a customer to see the ledger behind the balance.",
        empty_message="No customer has a balance on this date.",
    )


def _booker_quantities(start, end):
    """{order booker id (None for none): quantity sold less quantity returned} in the period."""
    quantities = {}
    sales = Sale.objects.filter(range_q(start, end)).prefetch_related("items")
    for sale in sales.iterator(chunk_size=500):
        quantities[sale.order_booker_id] = quantities.get(sale.order_booker_id, ZERO_QTY) + invoice_quantity(sale)
    sale_returns = SaleReturn.objects.filter(range_q(start, end)).select_related("sale").prefetch_related("items")
    for sale_return in sale_returns.iterator(chunk_size=500):
        booker_id = sale_return.sale.order_booker_id
        # invoice_quantity() hands a return back as a negative number.
        quantities[booker_id] = quantities.get(booker_id, ZERO_QTY) + invoice_quantity(sale_return)
    return quantities


def build_sales_by_order_booker(start, end, params):
    billed = (
        Sale.objects.filter(range_q(start, end))
        .values("order_booker_id")
        .annotate(
            invoices=Count("id"),
            customers=Count("customer_id", distinct=True),
            total=_money_sum("total_amount"),
            tax=_money_sum("tax_amount"),
            paid=_money_sum("paid_amount"),
            due=_money_sum(_sale_due_expression()),
        )
        .order_by()
    )
    returned = (
        SaleReturn.objects.filter(range_q(start, end))
        .values("sale__order_booker_id")
        .annotate(
            net=_money_sum("net_amount"),
            total=_money_sum("total_amount"),
            receivable=_money_sum("total_amount", filter=Q(refund_method=SaleReturn.RECEIVABLE)),
        )
        .order_by()
    )
    quantities = _booker_quantities(start, end)
    names = dict(OrderBooker.objects.values_list("id", "name"))

    rows = {}

    def row_for(booker_id):
        return rows.setdefault(
            booker_id,
            {
                "booker": names.get(booker_id, "No order booker"),
                "booker_url": f"{reverse('customer_list')}?order_booker={booker_id}" if booker_id else "",
                "invoices": 0,
                "customers": 0,
                "quantity": ZERO_QTY,
                "billed": ZERO,
                "returns": ZERO,
                "net_sales": ZERO,
                "paid": ZERO,
                "due": ZERO,
            },
        )

    for source in billed:
        row = row_for(source["order_booker_id"])
        row["invoices"] = source["invoices"]
        row["customers"] = source["customers"]
        row["billed"] += source["total"]
        row["net_sales"] += source["total"] - source["tax"]
        row["paid"] += source["paid"]
        row["due"] += source["due"]
    for source in returned:
        row = row_for(source["sale__order_booker_id"])
        row["returns"] += source["total"]
        row["net_sales"] -= source["net"]
        row["due"] = max(row["due"] - source["receivable"], ZERO)
    for booker_id, quantity in quantities.items():
        row_for(booker_id)["quantity"] += quantity

    result = []
    for row in rows.values():
        for key in ("billed", "returns", "net_sales", "paid", "due"):
            row[key] = quantize_money(row[key])
        result.append(row)
    # Named order bookers by net sales; the sales nobody is recorded against stay last.
    result.sort(key=lambda row: (row["booker_url"] == "", -row["net_sales"], row["booker"]))

    totals = {}
    if result:
        for key in ("invoices", "billed", "returns", "net_sales", "paid", "due"):
            totals[key] = sum((row[key] for row in result), 0 if key == "invoices" else ZERO)
        totals["quantity"] = sum((row["quantity"] for row in result), ZERO_QTY)
    # Quantities of different units are added as plain numbers, so they are shown without a unit.
    for row in [*result, totals]:
        if "quantity" in row:
            row["quantity"] = format_qty(row["quantity"])

    named = [row for row in result if row["booker_url"]]
    unassigned = next((row for row in result if not row["booker_url"]), None)
    return Report(
        columns=[
            Column("booker", "Order Booker", url_key="booker_url"),
            Column("invoices", "Invoices", INT),
            Column("customers", "Customers", INT),
            Column("quantity", QTY_LABEL, INT),
            Column("billed", "Billed", MONEY),
            Column("returns", "Returns", MONEY),
            Column("net_sales", "Net Sales", MONEY),
            Column("paid", "Paid", MONEY),
            Column("due", "Due on Invoices", MONEY),
        ],
        rows=result,
        totals=totals,
        cards=[
            _card("Order Bookers", len(named), INT, "slate", "With sales or returns in these dates"),
            _card("Net Sales", totals.get("net_sales", ZERO), accent="primary", meta="After discounts and returns, before tax"),
            _card("Billed", totals.get("billed", ZERO), accent="info", meta="Invoice totals, tax included"),
            _card(
                "No Order Booker",
                unassigned["billed"] if unassigned else ZERO,
                accent="warning",
                meta="Billed on sales with nobody recorded",
            ),
        ],
        note=(
            "Each sale counts for the order booker recorded on it when it was made. Billed and Returns include tax; "
            "Net Sales excludes tax. Open an order booker to see their customers."
        ),
        empty_message="No sales in this period.",
    )
