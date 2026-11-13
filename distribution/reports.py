"""The Distribution reports, built for the report framework of ``pos.services.reporting``:
the same page shell, date shortcuts and CSV export as every other report."""
from decimal import Decimal

from django.db.models import Prefetch
from django.urls import reverse

from pos.models import format_qty, quantize_money, quantize_qty
from pos.services.reporting import INT, MONEY, QTY, Column, Report, _card, _select_filter, pack_formatter

from .models import LoadForm, Recovery, SalesOrder, SalesOrderItem, StockReturn, StockReturnItem
from .services.loads import load_sheet, settlement_figures
from .services.stock_returns import damaged_held

ZERO = Decimal("0")


def _in_range(queryset, field, start, end):
    if start:
        queryset = queryset.filter(**{f"{field}__gte": start})
    if end:
        queryset = queryset.filter(**{f"{field}__lte": end})
    return queryset


def _choice(params, name, options, default):
    value = (params.get(name) or "").strip()
    return value if value in dict(options) else default


# ------------------------------------------------------------------- order booking
BOOKING_GROUPS = (("order_booker", "By order booker"), ("route", "By route"), ("date", "By day"))


def build_order_booking(start, end, params):
    by = _choice(params, "by", BOOKING_GROUPS, "order_booker")
    orders = _in_range(SalesOrder.objects.all(), "order_date", start, end).select_related("route", "order_booker").prefetch_related(
        Prefetch("items", queryset=SalesOrderItem.objects.all())
    )
    groups = {}
    for order in orders:
        if by == "order_booker":
            key, label = (order.order_booker_id or 0), (order.order_booker.name if order.order_booker_id else "No order booker")
        elif by == "route":
            key, label = (order.route_id or 0), (order.route.label if order.route_id else "No route")
        else:
            key, label = order.order_date, order.order_date.strftime("%d %b %Y")
        row = groups.setdefault(
            key,
            {"group": label, "orders": 0, "booked": ZERO, "invoiced": ZERO, "short": ZERO, "pending": ZERO, "cancelled": 0, "_sort": label if by != "date" else key},
        )
        if order.status == SalesOrder.CANCELLED:
            row["cancelled"] += 1
            continue
        row["orders"] += 1
        total = order.total_amount
        row["booked"] += total
        if order.status == SalesOrder.INVOICED:
            invoiced = order.invoiced_amount
            row["invoiced"] += invoiced
            row["short"] += total - invoiced
        else:
            row["pending"] += total
    rows = sorted(groups.values(), key=lambda row: row["_sort"], reverse=(by == "date"))
    for row in rows:
        for key in ("booked", "invoiced", "short", "pending"):
            row[key] = quantize_money(row[key])
        row["fill"] = quantize_money(row["invoiced"] / (row["invoiced"] + row["short"]) * 100) if (row["invoiced"] + row["short"]) else ZERO
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("booked", "invoiced", "short", "pending")}
    totals["orders"] = sum(row["orders"] for row in rows)
    totals["cancelled"] = sum(row["cancelled"] for row in rows)
    fill_base = totals["invoiced"] + totals["short"]
    fill = quantize_money(totals["invoiced"] / fill_base * 100) if fill_base else ZERO
    return Report(
        columns=[
            Column("group", dict(BOOKING_GROUPS)[by].replace("By ", "").capitalize()),
            Column("orders", "Orders", INT),
            Column("booked", "Booked", MONEY),
            Column("invoiced", "Invoiced", MONEY),
            Column("short", "Short", MONEY),
            Column("pending", "Still booked", MONEY),
            Column("fill", "Filled %", "percent"),
            Column("cancelled", "Cancelled", INT),
        ],
        rows=rows,
        totals={**totals, "fill": fill},
        cards=[
            _card("Orders booked", totals["orders"], INT, "slate", f"{totals['cancelled']} cancelled"),
            _card("Booked value", totals["booked"], accent="primary", meta="Orders not cancelled, at the booked rates"),
            _card("Invoiced", totals["invoiced"], accent="success", meta=f"{fill}% of what was ordered on invoiced orders"),
            _card("Short", totals["short"], accent="danger", meta="Ordered but not in stock when invoiced"),
        ],
        filters=[_select_filter("by", "Group", by, BOOKING_GROUPS)],
        note="Booked and short are at the rates on the order, before any bill discount or tax.",
    )


# --------------------------------------------------------------------- load sheets
def _load_rows(start, end, params):
    loads = _in_range(LoadForm.objects.all(), "load_date", start, end).select_related("route", "delivery_man", "vehicle").order_by("-load_date", "-id")
    rows = []
    for load in loads:
        sheet = load_sheet(load)
        figures = settlement_figures(load)
        rows.append(
            {
                "load": load,
                "number": load.display_number,
                "number_url": reverse("distribution_load_detail", args=[load.pk]),
                "date": load.load_date.strftime("%d %b %Y"),
                "route": load.route.label if load.route_id else "",
                "delivery_man": load.delivery_man.name if load.delivery_man_id else "",
                "vehicle": str(load.vehicle) if load.vehicle_id else "",
                "invoices": sheet["invoice_count"],
                "cartons": sheet["total_cartons"],
                "bill_value": figures["bill_value"],
                "returns": figures["returns_total"],
                "delivered": figures["delivered"],
                "recovered": figures["recovered"],
                "cash_expected": figures["cash_expected"],
                "cash_received": quantize_money(load.cash_received) if load.is_settled else None,
                "variance": quantize_money(load.cash_variance) if load.is_settled else None,
                "status": load.get_status_display(),
            }
        )
    return rows


def build_load_sheets(start, end, params):
    rows = _load_rows(start, end, params)
    money = ("bill_value", "returns", "delivered", "recovered", "cash_expected")
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in money}
    totals["invoices"] = sum(row["invoices"] for row in rows)
    totals["cartons"] = sum((row["cartons"] for row in rows), ZERO)
    settled = [row for row in rows if row["variance"] is not None]
    totals["cash_received"] = quantize_money(sum((row["cash_received"] for row in settled), ZERO))
    totals["variance"] = quantize_money(sum((row["variance"] for row in settled), ZERO))
    return Report(
        columns=[
            Column("number", "Load", url_key="number_url"),
            Column("date", "Date"),
            Column("route", "Route"),
            Column("delivery_man", "Delivery man", note_key="vehicle"),
            Column("invoices", "Invoices", INT),
            Column("cartons", "Cartons", QTY),
            Column("bill_value", "Bill value", MONEY),
            Column("returns", "Came back", MONEY),
            Column("delivered", "Delivered", MONEY),
            Column("recovered", "Recovered", MONEY),
            Column("cash_expected", "Cash expected", MONEY),
            Column("cash_received", "Cash in", MONEY),
            Column("variance", "Short / over", MONEY),
            Column("status", "Status"),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Loads", len(rows), INT, "slate", f"{len(settled)} settled"),
            _card("Bill value", totals["bill_value"], accent="primary", meta=f"{totals['invoices']} invoices, {format_qty(totals['cartons'])} cartons"),
            _card("Came back", totals["returns"], accent="danger", meta="Returns posted at settlement"),
            _card("Cash short / over", totals["variance"], accent="warning", meta="Received less expected, settled loads"),
        ],
        empty_message="No load forms for this period.",
    )


# --------------------------------------------------------- delivery man settlement
def build_delivery_settlement(start, end, params):
    groups = {}
    for row in _load_rows(start, end, params):
        load = row["load"]
        key = load.delivery_man_id or 0
        group = groups.setdefault(
            key,
            {
                "delivery_man": row["delivery_man"] or "No delivery man",
                "loads": 0,
                "open_loads": 0,
                "bill_value": ZERO,
                "returns": ZERO,
                "delivered": ZERO,
                "recovered": ZERO,
                "cash_expected": ZERO,
                "cash_received": ZERO,
                "variance": ZERO,
            },
        )
        group["loads"] += 1
        if not load.is_settled:
            group["open_loads"] += 1
        for name in ("bill_value", "returns", "delivered", "recovered", "cash_expected"):
            group[name] += row[name]
        if load.is_settled:
            group["cash_received"] += row["cash_received"]
            group["variance"] += row["variance"]
    rows = sorted(groups.values(), key=lambda row: row["delivery_man"].casefold())
    for row in rows:
        row["open_note"] = f"{row['open_loads']} not settled" if row["open_loads"] else ""
    money = ("bill_value", "returns", "delivered", "recovered", "cash_expected", "cash_received", "variance")
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in money}
    totals["loads"] = sum(row["loads"] for row in rows)
    return Report(
        columns=[
            Column("delivery_man", "Delivery man", note_key="open_note"),
            Column("loads", "Loads", INT),
            Column("bill_value", "Bill value", MONEY),
            Column("returns", "Came back", MONEY),
            Column("delivered", "Delivered", MONEY),
            Column("recovered", "Recovered", MONEY),
            Column("cash_expected", "Cash expected", MONEY),
            Column("cash_received", "Cash handed in", MONEY),
            Column("variance", "Short / over", MONEY),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Delivery men", len(rows), INT, "slate"),
            _card("Delivered", totals["delivered"], accent="primary", meta="Bill value less what came back"),
            _card("Recovered on the runs", totals["recovered"], accent="success"),
            _card("Cash short / over", totals["variance"], accent="warning", meta="Below zero: handed in less than expected"),
        ],
        note="Cash expected counts settled loads at the figure fixed when they were closed; loads still out show what is expected so far.",
        empty_message="No load forms for this period.",
    )


# ------------------------------------------------------------------------- recovery
RECOVERY_GROUPS = (("route", "By route"), ("person", "By person"), ("day", "By day"), ("customer", "By customer"))


def build_recovery(start, end, params):
    by = _choice(params, "by", RECOVERY_GROUPS, "route")
    recoveries = _in_range(Recovery.objects.all(), "collected_on", start, end).select_related("route", "customer")
    groups = {}
    for recovery in recoveries:
        if by == "route":
            key, label = recovery.route_id or 0, (recovery.route.label if recovery.route_id else "No route")
        elif by == "person":
            key = (recovery.collected_by, recovery.collector_name)
            label = recovery.collector_label
        elif by == "customer":
            key, label = recovery.customer_id, recovery.customer.name
        else:
            key, label = recovery.collected_on, recovery.collected_on.strftime("%d %b %Y")
        row = groups.setdefault(key, {"group": label, "count": 0, "cash": ZERO, "bank": ZERO, "total": ZERO, "_sort": key if by == "day" else label.casefold()})
        row["count"] += 1
        amount = Decimal(recovery.amount)
        row["cash" if recovery.payment_method == Recovery.CASH else "bank"] += amount
        row["total"] += amount
    rows = sorted(groups.values(), key=lambda row: row["_sort"], reverse=(by == "day"))
    for row in rows:
        for key in ("cash", "bank", "total"):
            row[key] = quantize_money(row[key])
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("cash", "bank", "total")}
    totals["count"] = sum(row["count"] for row in rows)
    return Report(
        columns=[
            Column("group", dict(RECOVERY_GROUPS)[by].replace("By ", "").capitalize()),
            Column("count", "Receipts", INT),
            Column("cash", "Cash", MONEY),
            Column("bank", "Bank", MONEY),
            Column("total", "Recovered", MONEY),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Recovered", totals["total"], accent="success", meta=f"{totals['count']} receipts"),
            _card("In cash", totals["cash"], accent="primary"),
            _card("By bank", totals["bank"], accent="info"),
        ],
        filters=[_select_filter("by", "Group", by, RECOVERY_GROUPS)],
        empty_message="No recoveries recorded for this period.",
    )


# -------------------------------------------------------------------- stock returns
RETURN_KINDS = (("", "Market and supplier returns"), (StockReturn.FROM_CUSTOMER, "Market returns only"), (StockReturn.TO_SUPPLIER, "Returns to suppliers only"))
CONDITIONS = (("", "Good and damaged"), (StockReturnItem.SALEABLE, "Good only"), (StockReturnItem.DAMAGED, "Damaged only"))


def build_stock_returns(start, end, params):
    kind = _choice(params, "kind", RETURN_KINDS, "")
    condition = _choice(params, "condition", CONDITIONS, "")
    items = StockReturnItem.objects.select_related("stock_return__customer", "stock_return__supplier", "product")
    items = _in_range(items, "stock_return__return_date", start, end)
    if kind:
        items = items.filter(stock_return__kind=kind)
    if condition:
        items = items.filter(condition=condition)
    rows = []
    for item in items.order_by("-stock_return__return_date", "-stock_return_id", "id"):
        stock_return = item.stock_return
        rows.append(
            {
                "date": stock_return.return_date.strftime("%d %b %Y"),
                "number": stock_return.display_number,
                "number_url": reverse("distribution_return_detail", args=[stock_return.pk]),
                "kind": "Market return" if stock_return.kind == StockReturn.FROM_CUSTOMER else "To supplier",
                "party": stock_return.party_name,
                "product": item.product.display_name,
                "quantity": item.base_quantity,
                "unit": item.product.unit,
                "packs": pack_formatter(item.product),
                "condition": item.get_condition_display(),
                "amount": quantize_money(item.line_amount),
                "cost": quantize_money(item.cost_amount),
                "reason": stock_return.reason,
            }
        )
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("amount", "cost")} if kind else {}
    market = [row for row in rows if row["kind"] == "Market return"]
    supplier = [row for row in rows if row["kind"] != "Market return"]
    damaged = [row for row in market if row["condition"] != "Good"]
    return Report(
        columns=[
            Column("date", "Date"),
            Column("number", "Return", url_key="number_url"),
            Column("kind", "Type"),
            Column("party", "Party", note_key="reason"),
            Column("product", "Product"),
            Column("quantity", "Quantity", QTY, unit_key="unit", packs_key="packs"),
            Column("condition", "Condition"),
            Column("amount", "Credited / charged", MONEY),
            Column("cost", "Stock value", MONEY),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Market returns", quantize_money(sum((row["amount"] for row in market), ZERO)), accent="danger", meta=f"{len(market)} lines credited to customers"),
            _card("Damaged taken back", quantize_money(sum((row["cost"] for row in damaged), ZERO)), accent="warning", meta="At cost, held for claims"),
            _card("Returned to suppliers", quantize_money(sum((row["amount"] for row in supplier), ZERO)), accent="info", meta=f"{len(supplier)} lines charged to suppliers"),
        ],
        filters=[_select_filter("kind", "Show", kind, RETURN_KINDS), _select_filter("condition", "Condition", condition, CONDITIONS)],
        note="" if kind else "Totals show when one kind of return is listed: market returns and supplier returns move money in opposite directions.",
        empty_message="No stock returns for this period.",
    )


def build_damaged_claims(start, end, params):
    items = StockReturnItem.objects.filter(condition=StockReturnItem.DAMAGED).select_related("stock_return", "product")
    held = damaged_held()
    groups = {}
    for item in _in_range(items, "stock_return__return_date", start, end):
        row = groups.setdefault(item.product_id, {"product_obj": item.product, "in_qty": ZERO, "in_cost": ZERO, "out_qty": ZERO, "out_cost": ZERO})
        if item.stock_return.kind == StockReturn.FROM_CUSTOMER:
            row["in_qty"] += item.base_quantity
            row["in_cost"] += Decimal(item.cost_amount)
        else:
            row["out_qty"] += item.base_quantity
            row["out_cost"] += Decimal(item.cost_amount)
    # Products still holding damaged stock show even when nothing moved in the period.
    from pos.models import Product

    missing = [pk for pk, (qty, _) in held.items() if qty > 0 and pk not in groups]
    for product in Product.objects.filter(pk__in=missing):
        groups[product.pk] = {"product_obj": product, "in_qty": ZERO, "in_cost": ZERO, "out_qty": ZERO, "out_cost": ZERO}
    rows = []
    for pk, row in groups.items():
        product = row.pop("product_obj")
        held_qty, held_cost = held.get(pk, (ZERO, ZERO))
        rows.append(
            {
                "product": product.display_name,
                "unit": product.unit,
                "packs": pack_formatter(product),
                "in_qty": quantize_qty(row["in_qty"]),
                "in_cost": quantize_money(row["in_cost"]),
                "out_qty": quantize_qty(row["out_qty"]),
                "out_cost": quantize_money(row["out_cost"]),
                "held_qty": held_qty,
                "held_cost": held_cost,
            }
        )
    rows.sort(key=lambda row: row["product"].casefold())
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("in_cost", "out_cost", "held_cost")}
    return Report(
        columns=[
            Column("product", "Product"),
            Column("in_qty", "Taken back damaged", QTY, unit_key="unit", packs_key="packs"),
            Column("in_cost", "Value in", MONEY),
            Column("out_qty", "Sent to supplier", QTY, unit_key="unit", packs_key="packs"),
            Column("out_cost", "Value out", MONEY),
            Column("held_qty", "Held now", QTY, unit_key="unit", packs_key="packs"),
            Column("held_cost", "Held at cost", MONEY),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Taken back damaged", totals["in_cost"], accent="warning", meta="At cost, this period"),
            _card("Claimed from suppliers", totals["out_cost"], accent="info", meta="Damaged stock sent back, this period"),
            _card("Held for claims now", totals["held_cost"], accent="danger", meta="The balance of the Damaged Stock account"),
        ],
        note="Held now is every damaged item taken back and not yet sent to a supplier, whatever the dates.",
        empty_message="No damaged stock taken back or held.",
    )
