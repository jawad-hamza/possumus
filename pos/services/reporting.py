"""Reporting queries shared by the dashboard, the reports centre and every report page.

Every figure built here follows the same rules:

* dates are LOCAL dates: a day is turned into aware datetime bounds in the active
  time zone, which also keeps the filter usable by an index on created_at;
* sales are NET of SaleReturn (net, tax and cost). A return counts in the period it
  was recorded in, the same way the ledger books it;
* sums, counts and groupings happen in the database, never over python lists of
  whole querysets.
"""
import calendar
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib.contenttypes.models import ContentType
from django.db.models import (
    Case,
    CharField,
    Count,
    DateTimeField,
    DecimalField,
    DurationField,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from accounts.models import Expense, PartyLedgerEntry
from accounts.services.expense_alerts import monthly_bill_alerts
from pos.models import (
    PACK_PIECE,
    Category,
    Product,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    PurchaseReturnItem,
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    StockAdjustment,
    UserSessionLog,
    compose_display_name,
    format_qty,
    quantize_money,
    quantize_qty,
)
from pos.services.business_rules import (
    expired_products,
    get_system_settings,
    low_stock_products,
    near_expiry_products,
    sale_discount_amount,
    sale_tax_amount,
    sale_tax_label,
)

ZERO = Decimal("0")
ONE = Decimal("1")
# Percentages are applied by multiplying with 0.01. SQLite stores whole decimals as
# integers, so "rate / 100" would silently become integer division there.
PERCENT = Decimal("0.01")
MONEY_FIELD = DecimalField(max_digits=18, decimal_places=2)
QTY_FIELD = DecimalField(max_digits=18, decimal_places=3)

TEXT = "text"
MONEY = "money"
QTY = "qty"
INT = "int"
PERCENT_KIND = "percent"
DATETIME = "datetime"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_report_date(value, default=None):
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return default


def parse_report_range(params, default_start=None, default_end=None):
    """Read start/end from a querystring. from/to are accepted for old bookmarks.

    ``?all=1`` means no date limit at all.
    """
    if params.get("all"):
        return None, None
    start = parse_report_date(params.get("start") or params.get("from"), default_start)
    end = parse_report_date(params.get("end") or params.get("to"), default_end if default_end is not None else start)
    if start and end and end < start:
        start, end = end, start
    return start, end


def local_day_bounds(start=None, end=None):
    """Aware datetimes [from, until) covering the local dates start..end inclusive."""
    tz = timezone.get_current_timezone()
    lower = timezone.make_aware(datetime.combine(start, time.min), tz) if start else None
    upper = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz) if end else None
    return lower, upper


def range_q(start=None, end=None, field_name="created_at"):
    lower, upper = local_day_bounds(start, end)
    query = Q()
    if lower is not None:
        query &= Q(**{f"{field_name}__gte": lower})
    if upper is not None:
        query &= Q(**{f"{field_name}__lt": upper})
    return query


def range_label(start, end):
    if not start and not end:
        return "All time"
    if start and end:
        if start == end:
            return start.strftime("%d %b %Y")
        return f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}"
    if start:
        return f"From {start.strftime('%d %b %Y')}"
    return f"Up to {end.strftime('%d %b %Y')}"


def quick_ranges(today=None):
    today = today or timezone.localdate()
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    return [
        {"label": "Today", "start": today, "end": today},
        {"label": "Yesterday", "start": today - timedelta(days=1), "end": today - timedelta(days=1)},
        {"label": "Last 7 days", "start": today - timedelta(days=6), "end": today},
        {"label": "This month", "start": month_start, "end": today},
        {"label": "Last month", "start": last_month_end.replace(day=1), "end": last_month_end},
        {"label": "This year", "start": today.replace(month=1, day=1), "end": today},
    ]


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------

def _money_sum(expression, **kwargs):
    return Coalesce(Sum(expression, output_field=MONEY_FIELD, **kwargs), Value(ZERO), output_field=MONEY_FIELD)


def _qty_sum(expression, **kwargs):
    return Coalesce(Sum(expression, output_field=QTY_FIELD, **kwargs), Value(ZERO), output_field=QTY_FIELD)


def _sale_due_expression():
    return Case(
        When(total_amount__gt=F("paid_amount"), then=F("total_amount") - F("paid_amount")),
        default=Value(ZERO),
        output_field=MONEY_FIELD,
    )


def _base_qty_expression():
    """A sale, purchase or return line's quantity in the product's base unit (packs x pieces per pack)."""
    return ExpressionWrapper(F("quantity") * F("unit_factor"), output_field=QTY_FIELD)


def _item_cost_expression():
    return ExpressionWrapper(F("cost_price") * F("quantity"), output_field=MONEY_FIELD)


def _item_net_expression():
    """A sale line after its own discount and its share of the order discount, before tax."""
    return ExpressionWrapper(
        F("price")
        * F("quantity")
        * (Value(ONE) - F("discount_percent") * Value(PERCENT))
        * (Value(ONE) - F("sale__discount_rate") * Value(PERCENT)),
        output_field=MONEY_FIELD,
    )


def _percent(part, whole):
    part = Decimal(part or 0)
    whole = Decimal(whole or 0)
    if not whole:
        return ZERO
    return quantize_money(part / whole * Decimal("100"))


# ---------------------------------------------------------------------------
# Sales totals (net of returns)
# ---------------------------------------------------------------------------

def _assemble_sales_totals(sales, cost, returns):
    gross_total = quantize_money(sales["total"])
    gross_tax = quantize_money(sales["tax"])
    gross_net = quantize_money(gross_total - gross_tax)
    gross_cost = quantize_money(cost)

    returns_net = quantize_money(returns["net"])
    returns_tax = quantize_money(returns["tax"])
    returns_total = quantize_money(returns["total"])
    returns_cost = quantize_money(returns["cost"])

    net_sales = quantize_money(gross_net - returns_net)
    cost_total = quantize_money(gross_cost - returns_cost)
    invoices = sales["invoices"] or 0
    return {
        "invoices": invoices,
        "gross_sales": gross_net,
        "gross_total": gross_total,
        "gross_cost": gross_cost,
        "returns_count": returns["count"] or 0,
        "returns_net": returns_net,
        "returns_tax": returns_tax,
        "returns_total": returns_total,
        "returns_cost": returns_cost,
        "net_sales": net_sales,
        "tax": quantize_money(gross_tax - returns_tax),
        "total": quantize_money(gross_total - returns_total),
        "cost": cost_total,
        "gross_profit": quantize_money(net_sales - cost_total),
        # Money actually kept: collected on invoices less cash/bank refunds.
        "paid": quantize_money(Decimal(sales["paid"]) - Decimal(returns["refunded"])),
        # A return settled against the receivable lowers what is still owed.
        "due": quantize_money(max(Decimal(sales["due"]) - Decimal(returns["receivable"]), ZERO)),
        "avg_invoice": quantize_money(net_sales / invoices) if invoices else ZERO,
    }


def sales_totals_by_period(periods, *, sold_by=None, customer=None):
    """Totals for several date ranges at once.

    ``periods`` maps a name to ``(start, end)`` local dates (either may be None).
    Three queries run whatever the number of periods: invoices, line costs, returns.
    """
    sale_filter = Q()
    item_filter = Q()
    return_filter = Q()
    if sold_by is not None:
        sale_filter &= Q(sold_by=sold_by)
        item_filter &= Q(sale__sold_by=sold_by)
        return_filter &= Q(sale__sold_by=sold_by)
    if customer is not None:
        sale_filter &= Q(customer=customer)
        item_filter &= Q(sale__customer=customer)
        return_filter &= Q(sale__customer=customer)

    sale_aggregates = {}
    item_aggregates = {}
    return_aggregates = {}
    refunded = Q(refund_method__in=(SaleReturn.CASH, SaleReturn.BANK))
    for name, (start, end) in periods.items():
        on_sale = range_q(start, end)
        on_item = range_q(start, end, "sale__created_at")
        sale_aggregates[f"{name}__invoices"] = Count("id", filter=on_sale)
        sale_aggregates[f"{name}__total"] = _money_sum("total_amount", filter=on_sale)
        sale_aggregates[f"{name}__tax"] = _money_sum("tax_amount", filter=on_sale)
        sale_aggregates[f"{name}__paid"] = _money_sum("paid_amount", filter=on_sale)
        sale_aggregates[f"{name}__due"] = _money_sum(_sale_due_expression(), filter=on_sale)
        item_aggregates[name] = _money_sum(_item_cost_expression(), filter=on_item)
        return_aggregates[f"{name}__count"] = Count("id", filter=on_sale)
        return_aggregates[f"{name}__net"] = _money_sum("net_amount", filter=on_sale)
        return_aggregates[f"{name}__tax"] = _money_sum("tax_amount", filter=on_sale)
        return_aggregates[f"{name}__total"] = _money_sum("total_amount", filter=on_sale)
        return_aggregates[f"{name}__cost"] = _money_sum("cost_amount", filter=on_sale)
        return_aggregates[f"{name}__refunded"] = _money_sum("total_amount", filter=on_sale & refunded)
        return_aggregates[f"{name}__receivable"] = _money_sum(
            "total_amount", filter=on_sale & Q(refund_method=SaleReturn.RECEIVABLE)
        )

    # Only scan the rows that can belong to one of the periods.
    starts = [start for start, _ in periods.values()]
    ends = [end for _, end in periods.values()]
    outer_start = min(starts) if starts and all(starts) else None
    outer_end = max(ends) if ends and all(ends) else None

    sale_row = Sale.objects.filter(sale_filter & range_q(outer_start, outer_end)).aggregate(**sale_aggregates)
    item_row = SaleItem.objects.filter(
        item_filter & range_q(outer_start, outer_end, "sale__created_at")
    ).aggregate(**item_aggregates)
    return_row = SaleReturn.objects.filter(return_filter & range_q(outer_start, outer_end)).aggregate(**return_aggregates)

    result = {}
    for name in periods:
        result[name] = _assemble_sales_totals(
            {key: sale_row[f"{name}__{key}"] for key in ("invoices", "total", "tax", "paid", "due")},
            item_row[name],
            {
                key: return_row[f"{name}__{key}"]
                for key in ("count", "net", "tax", "total", "cost", "refunded", "receivable")
            },
        )
    return result


def sales_totals(start=None, end=None, **filters):
    return sales_totals_by_period({"range": (start, end)}, **filters)["range"]


def sales_trend(end_date, days=7):
    """Net sales and gross profit per local day. One grouped query per table, not per day."""
    start_date = end_date - timedelta(days=days - 1)
    in_range = range_q(start_date, end_date)

    net_by_day = {}
    profit_by_day = {}

    def add(day, net=ZERO, cost=ZERO):
        net_by_day[day] = net_by_day.get(day, ZERO) + Decimal(net or 0)
        profit_by_day[day] = profit_by_day.get(day, ZERO) + Decimal(net or 0) - Decimal(cost or 0)

    sale_rows = (
        Sale.objects.filter(in_range)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(net=_money_sum(F("total_amount") - F("tax_amount")))
        .order_by()
    )
    for row in sale_rows:
        add(row["day"], net=row["net"])

    cost_rows = (
        SaleItem.objects.filter(range_q(start_date, end_date, "sale__created_at"))
        .annotate(day=TruncDate("sale__created_at"))
        .values("day")
        .annotate(cost=_money_sum(_item_cost_expression()))
        .order_by()
    )
    for row in cost_rows:
        add(row["day"], cost=row["cost"])

    return_rows = (
        SaleReturn.objects.filter(in_range)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(net=_money_sum("net_amount"), cost=_money_sum("cost_amount"))
        .order_by()
    )
    for row in return_rows:
        add(row["day"], net=-row["net"], cost=-row["cost"])

    labels, net_sales, gross_profit = [], [], []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        labels.append(day.strftime("%d %b"))
        net_sales.append(float(quantize_money(net_by_day.get(day, ZERO))))
        gross_profit.append(float(quantize_money(profit_by_day.get(day, ZERO))))
    return {"labels": labels, "net_sales": net_sales, "gross_profit": gross_profit}


def sale_number(sale):
    """The printed invoice number when the sale has one, otherwise its id."""
    return getattr(sale, "display_number", "") or f"#{sale.pk}"


def annotate_sale_rows(sales, fallback_tax_label=""):
    """Per-invoice figures for a PAGE of sales (items prefetched), plus what was returned."""
    sales = list(sales)
    returned = {
        row["sale_id"]: row
        for row in SaleReturn.objects.filter(sale_id__in=[sale.id for sale in sales])
        .values("sale_id")
        .annotate(total=_money_sum("total_amount"), net=_money_sum("net_amount"), cost=_money_sum("cost_amount"))
        .order_by()
    }
    for sale in sales:
        items = list(sale.items.all())
        subtotal = sum((item.line_total for item in items), ZERO)
        cost_total = sum((item.line_cost for item in items), ZERO)
        tax_amount = sale_tax_amount(sale, subtotal=subtotal, items=items)
        sale_returns = returned.get(sale.id, {})

        sale.report_number = sale_number(sale)
        sale.report_subtotal = quantize_money(subtotal)
        sale.report_discount_amount = sale_discount_amount(subtotal, sale.discount_rate)
        sale.report_tax_amount = tax_amount
        sale.report_tax_label = sale_tax_label(sale, fallback_tax_label)
        sale.report_net_sales = quantize_money(Decimal(sale.total_amount or 0) - tax_amount)
        sale.report_cost_total = quantize_money(cost_total)
        sale.report_gross_profit = quantize_money(sale.report_net_sales - sale.report_cost_total)
        sale.report_balance_due = sale.balance_due
        sale.report_returned_total = quantize_money(sale_returns.get("total", ZERO))
        sale.report_net_gross_profit = quantize_money(
            sale.report_gross_profit - Decimal(sale_returns.get("net", ZERO)) + Decimal(sale_returns.get("cost", ZERO))
        )
    return sales


# ---------------------------------------------------------------------------
# Best sellers, top customers
# ---------------------------------------------------------------------------

def best_selling_products(start=None, end=None, limit=10):
    """Quantity sold less quantity returned in the period, grouped in the database."""
    sold = (
        SaleItem.objects.filter(range_q(start, end, "sale__created_at"))
        .values("product__id", "product__name", "product__unit", "product__variant_label", "product__variant_group_id")
        .annotate(total_qty=_qty_sum(_base_qty_expression()))
        .order_by()
    )
    rows = {}
    for row in sold:
        row = dict(row)
        # A size of a grouped item is reported as "Bread - Small".
        row["product__name"] = compose_display_name(
            row["product__name"], row.pop("product__variant_label"), row.pop("product__variant_group_id")
        )
        rows[row["product__id"]] = row
    returned = (
        SaleReturnItem.objects.filter(range_q(start, end, "sale_return__created_at"))
        .values("product__id")
        .annotate(total_qty=_qty_sum(_base_qty_expression()))
        .order_by()
    )
    for row in returned:
        if row["product__id"] in rows:
            rows[row["product__id"]]["total_qty"] -= row["total_qty"]
    ranked = sorted(
        (row for row in rows.values() if row["total_qty"] > 0),
        key=lambda row: (-row["total_qty"], row["product__name"]),
    )[:limit]
    products = Product.objects.in_bulk([row["product__id"] for row in ranked])
    for row in ranked:
        product = products.get(row["product__id"])
        # "12 Ctn 3 Box" for a packed product; empty for a plain one, which shows the number and its unit.
        row["qty_display"] = product.format_quantity(row["total_qty"]) if product is not None and product.has_packaging else ""
    return ranked


def top_customers(start=None, end=None, limit=10):
    billed = (
        Sale.objects.filter(range_q(start, end), customer__isnull=False)
        .values("customer__id", "customer__name")
        .annotate(total_spent=_money_sum("total_amount"), invoices=Count("id"))
        .order_by()
    )
    rows = {row["customer__id"]: dict(row) for row in billed}
    returned = (
        SaleReturn.objects.filter(range_q(start, end), sale__customer__isnull=False)
        .values("sale__customer__id")
        .annotate(total=_money_sum("total_amount"))
        .order_by()
    )
    for row in returned:
        if row["sale__customer__id"] in rows:
            rows[row["sale__customer__id"]]["total_spent"] -= row["total"]
    ranked = sorted(rows.values(), key=lambda row: (-row["total_spent"], row["customer__name"]))
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Party balances
# ---------------------------------------------------------------------------
# Sign convention (accounts.services.party_balance): debit - credit.
# Positive: the party owes us. Negative: we owe the party.

def with_ledger_balance(queryset):
    """Annotate customers or suppliers with ``ledger_balance`` in the same query."""
    content_type = ContentType.objects.get_for_model(queryset.model)
    balance = (
        PartyLedgerEntry.objects.filter(content_type=content_type, object_id=OuterRef("pk"))
        .order_by()
        .values("object_id")
        .annotate(balance=Sum("debit") - Sum("credit"))
        .values("balance")[:1]
    )
    return queryset.annotate(
        ledger_balance=Coalesce(Subquery(balance, output_field=MONEY_FIELD), Value(ZERO), output_field=MONEY_FIELD)
    )


def party_badge(party_kind, balance):
    balance = quantize_money(balance or 0)
    if balance == 0:
        return {"balance_type": "clear", "amount": ZERO}
    if party_kind == "customer":
        return {"balance_type": "receivable" if balance > 0 else "advance", "amount": abs(balance)}
    return {"balance_type": "payable" if balance < 0 else "advance", "amount": abs(balance)}


def apply_party_badges(parties, party_kind):
    for party in parties:
        badge = party_badge(party_kind, getattr(party, "ledger_balance", ZERO))
        party.balance_type = badge["balance_type"]
        party.balance_amount = badge["amount"]
    return parties


def party_balance_summary(model, *, active_only=True):
    """One grouped aggregate over the party sub-ledger for every customer or supplier."""
    content_type = ContentType.objects.get_for_model(model)
    entries = PartyLedgerEntry.objects.filter(content_type=content_type)
    if active_only:
        entries = entries.filter(object_id__in=model.objects.filter(is_active=True).values("pk"))
    balances = (
        entries.values("object_id")
        .annotate(balance=_money_sum("debit") - _money_sum("credit"))
        .order_by()
        .values_list("balance", flat=True)
    )
    summary = {"owed_to_us": ZERO, "owed_to_us_count": 0, "owed_by_us": ZERO, "owed_by_us_count": 0}
    for balance in balances:
        balance = Decimal(balance or 0)
        if balance > 0:
            summary["owed_to_us"] += balance
            summary["owed_to_us_count"] += 1
        elif balance < 0:
            summary["owed_by_us"] += -balance
            summary["owed_by_us_count"] += 1
    summary["owed_to_us"] = quantize_money(summary["owed_to_us"])
    summary["owed_by_us"] = quantize_money(summary["owed_by_us"])
    return summary


def filter_parties_by_balance(queryset, party_kind, balance_filter):
    """queryset must already carry ``ledger_balance``."""
    if balance_filter == "clear":
        return queryset.filter(ledger_balance=0)
    owes_us = queryset.filter(ledger_balance__gt=0)
    we_owe = queryset.filter(ledger_balance__lt=0)
    if party_kind == "customer":
        return {"receivable": owes_us, "advance": we_owe}.get(balance_filter, queryset)
    return {"payable": we_owe, "advance": owes_us}.get(balance_filter, queryset)


# ---------------------------------------------------------------------------
# Snapshot shared by the dashboard and the reports centre
# ---------------------------------------------------------------------------

def expense_due_total():
    due = Case(
        When(amount__gt=F("paid_amount"), then=F("amount") - F("paid_amount")),
        default=Value(ZERO),
        output_field=MONEY_FIELD,
    )
    return quantize_money(Expense.objects.aggregate(total=_money_sum(due))["total"])


def date_range_url(route_name, start=None, end=None, **extra):
    params = {}
    if start:
        params["start"] = start.isoformat()
    if end:
        params["end"] = end.isoformat()
    params.update({key: value for key, value in extra.items() if value not in (None, "")})
    base_url = reverse(route_name)
    if not params:
        return base_url
    return f"{base_url}?{urlencode(params)}"


def business_snapshot(today=None):
    """Everything the dashboard and the reports centre both show, computed once."""
    today = today or timezone.localdate()
    week_start = today - timedelta(days=6)
    month_start = today.replace(day=1)
    settings_obj = get_system_settings()

    totals = sales_totals_by_period(
        {
            "today": (today, today),
            "week": (week_start, today),
            "month": (month_start, today),
        }
    )
    low_stock = low_stock_products()
    near_expiry = near_expiry_products(settings_obj, today=today)
    expired = expired_products(settings_obj, today=today)
    month_purchase_total = Purchase.objects.filter(range_q(month_start, today)).aggregate(
        total=_money_sum("total_amount")
    )["total"]

    return {
        "today": today,
        "week_start": week_start,
        "month_start": month_start,
        "settings": settings_obj,
        "today_sales": totals["today"],
        "week_sales": totals["week"],
        "month_sales": totals["month"],
        "low_stock_products": low_stock,
        "low_stock_count": low_stock.count(),
        "near_expiry_products": near_expiry,
        "expired_products": expired,
        "near_expiry_count": near_expiry.count(),
        "expired_count": expired.count(),
        "month_purchase_total": quantize_money(month_purchase_total),
        "expense_due_total": expense_due_total(),
        "expense_alert_count": len(monthly_bill_alerts(today=today)),
        "best_selling_month": best_selling_products(month_start, today, limit=10),
        "today_report_url": date_range_url("daily_sales_report", today, today),
        "week_report_url": date_range_url("daily_sales_report", week_start, today),
        "month_report_url": date_range_url("daily_sales_report", month_start, today),
        "expiry_report_url": reverse("expiry_alert_report"),
    }


# ---------------------------------------------------------------------------
# Employee figures
# ---------------------------------------------------------------------------

def employee_sales_totals(start=None, end=None):
    """{user_id: totals} for every user with sales or returns in the range. Three queries."""
    sales = (
        Sale.objects.filter(range_q(start, end), sold_by__isnull=False)
        .values("sold_by")
        .annotate(
            invoices=Count("id"),
            total=_money_sum("total_amount"),
            tax=_money_sum("tax_amount"),
            paid=_money_sum("paid_amount"),
            due=_money_sum(_sale_due_expression()),
        )
        .order_by()
    )
    costs = dict(
        SaleItem.objects.filter(range_q(start, end, "sale__created_at"), sale__sold_by__isnull=False)
        .values("sale__sold_by")
        .annotate(cost=_money_sum(_item_cost_expression()))
        .order_by()
        .values_list("sale__sold_by", "cost")
    )
    refunded = Q(refund_method__in=(SaleReturn.CASH, SaleReturn.BANK))
    returns = {
        row["sale__sold_by"]: row
        for row in SaleReturn.objects.filter(range_q(start, end), sale__sold_by__isnull=False)
        .values("sale__sold_by")
        .annotate(
            count=Count("id"),
            net=_money_sum("net_amount"),
            tax=_money_sum("tax_amount"),
            total=_money_sum("total_amount"),
            cost=_money_sum("cost_amount"),
            refunded=_money_sum("total_amount", filter=refunded),
            receivable=_money_sum("total_amount", filter=Q(refund_method=SaleReturn.RECEIVABLE)),
        )
        .order_by()
    }
    no_sales = {"invoices": 0, "total": ZERO, "tax": ZERO, "paid": ZERO, "due": ZERO}
    no_returns = {"count": 0, "net": ZERO, "tax": ZERO, "total": ZERO, "cost": ZERO, "refunded": ZERO, "receivable": ZERO}

    sales_by_user = {row["sold_by"]: row for row in sales}
    result = {}
    for user_id in set(sales_by_user) | set(returns):
        result[user_id] = _assemble_sales_totals(
            sales_by_user.get(user_id, no_sales),
            costs.get(user_id, ZERO),
            returns.get(user_id, no_returns),
        )
    return result


def empty_sales_totals():
    return _assemble_sales_totals(
        {"invoices": 0, "total": ZERO, "tax": ZERO, "paid": ZERO, "due": ZERO},
        ZERO,
        {"count": 0, "net": ZERO, "tax": ZERO, "total": ZERO, "cost": ZERO, "refunded": ZERO, "receivable": ZERO},
    )


def employee_wage_totals(start=None, end=None):
    """{user_id: {"booked", "paid", "due", "count"}} in one grouped query."""
    due = Case(
        When(amount__gt=F("paid_amount"), then=F("amount") - F("paid_amount")),
        default=Value(ZERO),
        output_field=MONEY_FIELD,
    )
    expenses = Expense.objects.filter(employee__isnull=False)
    if start:
        expenses = expenses.filter(date__gte=start)
    if end:
        expenses = expenses.filter(date__lte=end)
    rows = (
        expenses.values("employee")
        .annotate(booked=_money_sum("amount"), paid=_money_sum("paid_amount"), due=_money_sum(due), count=Count("id"))
        .order_by()
    )
    return {
        row["employee"]: {
            "booked": quantize_money(row["booked"]),
            "paid": quantize_money(row["paid"]),
            "due": quantize_money(row["due"]),
            "count": row["count"],
        }
        for row in rows
    }


def employee_session_totals(start=None, end=None, now=None):
    """{user_id: {"duration": timedelta, "active": open sessions}} in one grouped query."""
    now = now or timezone.now()
    worked = ExpressionWrapper(
        Coalesce(F("logout_at"), Value(now), output_field=DateTimeField()) - F("login_at"),
        output_field=DurationField(),
    )
    rows = (
        UserSessionLog.objects.filter(range_q(start, end, "login_at"))
        .values("user")
        .annotate(
            # A logout stamped before its login (clock change) counts as no time.
            duration=Sum(worked, filter=Q(logout_at__isnull=True, login_at__lte=now) | Q(logout_at__gte=F("login_at"))),
            active=Count("id", filter=Q(logout_at__isnull=True)),
        )
        .order_by()
    )
    return {
        row["user"]: {"duration": row["duration"] or timedelta(0), "active": row["active"]}
        for row in rows
    }


# ---------------------------------------------------------------------------
# Tabular reports
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str = TEXT
    unit_key: str = ""
    url_key: str = ""
    note_key: str = ""
    # Row key holding a function that words a base quantity in packs ("12 Ctn 3 Box 5 pcs").
    # The CSV keeps the plain number and its unit so a spreadsheet can still add it up.
    packs_key: str = ""

    @property
    def is_numeric(self):
        return self.kind in (MONEY, QTY, INT, PERCENT_KIND)


@dataclass
class Report:
    columns: list
    rows: list
    totals: dict = field(default_factory=dict)
    cards: list = field(default_factory=list)
    filters: list = field(default_factory=list)
    note: str = ""
    empty_message: str = "Nothing to show for this period."


@dataclass(frozen=True)
class ReportSpec:
    slug: str
    title: str
    description: str
    icon: str
    group: str
    url_name: str
    builder: object = None
    # "range": start and end dates, "as_of": one date, "none": no dates at all,
    # "period": one day, month or year chosen from ``periods``.
    range_mode: str = "range"
    accent: str = "primary"
    # Label inside the switcher, where the group name already says "Sales" or "Stock".
    short_title: str = ""
    # For range_mode "period": ((code, label), ...), the first being the default.
    periods: tuple = ()

    @property
    def period_codes(self):
        return tuple(code for code, _ in self.periods)


def _card(label, value, kind=MONEY, accent="primary", meta=""):
    return {"label": label, "value": value, "kind": kind, "accent": accent, "meta": meta}


def _select_filter(name, label, value, options):
    return {"name": name, "label": label, "value": str(value or ""), "options": [(str(key), text) for key, text in options]}


def _profit_row(row):
    row["net_sales"] = quantize_money(row["net_sales"])
    row["cost"] = quantize_money(row["cost"])
    row["gross_profit"] = quantize_money(row["net_sales"] - row["cost"])
    row["margin"] = _percent(row["gross_profit"], row["net_sales"])
    return row


def _profit_totals(rows):
    net = sum((row["net_sales"] for row in rows), ZERO)
    cost = sum((row["cost"] for row in rows), ZERO)
    return {
        "net_sales": quantize_money(net),
        "cost": quantize_money(cost),
        "gross_profit": quantize_money(net - cost),
        "margin": _percent(net - cost, net),
    }


def _profit_cards(totals, first_card):
    return [
        first_card,
        _card("Net Sales", totals["net_sales"], accent="primary", meta="After discounts and returns, before tax"),
        _card("Cost", totals["cost"], accent="warning", meta="Cost of the goods sold"),
        _card("Gross Profit", totals["gross_profit"], accent="success", meta=f"Margin {totals['margin']}%"),
    ]


_SALES_BY_PRODUCT_FIELDS = (
    "product_id",
    "product__name",
    "product__variant_label",
    "product__variant_group_id",
    "product__sku",
    "product__unit",
    "product__category__name",
)


def build_sales_by_product(start, end, params):
    sold = (
        SaleItem.objects.filter(range_q(start, end, "sale__created_at"))
        .values(*_SALES_BY_PRODUCT_FIELDS)
        .annotate(qty=_qty_sum(_base_qty_expression()), net=_money_sum(_item_net_expression()), cost=_money_sum(_item_cost_expression()))
        .order_by()
    )
    returned = (
        SaleReturnItem.objects.filter(range_q(start, end, "sale_return__created_at"))
        .values(*_SALES_BY_PRODUCT_FIELDS)
        .annotate(qty=_qty_sum(_base_qty_expression()), net=_money_sum("net_amount"), cost=_money_sum("cost_amount"))
        .order_by()
    )

    rows = {}

    def row_for(source):
        return rows.setdefault(
            source["product_id"],
            {
                "product": compose_display_name(
                    source["product__name"], source["product__variant_label"], source["product__variant_group_id"]
                ),
                "sku": source["product__sku"],
                "category": source["product__category__name"] or "Uncategorised",
                "unit": source["product__unit"],
                "sold_qty": ZERO,
                "returned_qty": ZERO,
                "net_sales": ZERO,
                "cost": ZERO,
                "product_url": date_range_url("report_stock_card", start, end, product=source["product_id"]),
            },
        )

    for source in sold:
        row = row_for(source)
        row["sold_qty"] += source["qty"]
        row["net_sales"] += source["net"]
        row["cost"] += source["cost"]
    for source in returned:
        row = row_for(source)
        row["returned_qty"] += source["qty"]
        row["net_sales"] -= source["net"]
        row["cost"] -= source["cost"]

    result = []
    products = Product.objects.in_bulk(list(rows.keys()))
    for product_id, row in rows.items():
        row["net_qty"] = quantize_qty(row["sold_qty"] - row["returned_qty"])
        row["packer"] = pack_formatter(products.get(product_id))
        result.append(_profit_row(row))
    result.sort(key=lambda row: (-row["net_sales"], row["product"]))

    totals = _profit_totals(result)
    return Report(
        columns=[
            Column("product", "Product", url_key="product_url", note_key="sku"),
            Column("category", "Category"),
            Column("sold_qty", "Sold", QTY, unit_key="unit", packs_key="packer"),
            Column("returned_qty", "Returned", QTY, unit_key="unit", packs_key="packer"),
            Column("net_qty", "Net Qty", QTY, unit_key="unit", packs_key="packer"),
            Column("net_sales", "Net Sales", MONEY),
            Column("cost", "Cost", MONEY),
            Column("gross_profit", "Gross Profit", MONEY),
            Column("margin", "Margin", PERCENT_KIND),
        ],
        rows=result,
        totals=totals,
        cards=_profit_cards(totals, _card("Products Sold", len(result), INT, "slate", "Products with sales or returns")),
        note="Click a product to open its stock movement card for the same dates.",
    )


def build_sales_by_category(start, end, params):
    sold = (
        SaleItem.objects.filter(range_q(start, end, "sale__created_at"))
        .values("product__category_id", "product__category__name")
        .annotate(
            products=Count("product_id", distinct=True),
            net=_money_sum(_item_net_expression()),
            cost=_money_sum(_item_cost_expression()),
        )
        .order_by()
    )
    returned = (
        SaleReturnItem.objects.filter(range_q(start, end, "sale_return__created_at"))
        .values("product__category_id", "product__category__name")
        .annotate(net=_money_sum("net_amount"), cost=_money_sum("cost_amount"))
        .order_by()
    )
    rows = {}

    def row_for(source):
        return rows.setdefault(
            source["product__category_id"],
            {
                "category": source["product__category__name"] or "Uncategorised",
                "products": 0,
                "gross_sales": ZERO,
                "returns": ZERO,
                "net_sales": ZERO,
                "cost": ZERO,
            },
        )

    for source in sold:
        row = row_for(source)
        row["products"] = source["products"]
        row["gross_sales"] += source["net"]
        row["net_sales"] += source["net"]
        row["cost"] += source["cost"]
    for source in returned:
        row = row_for(source)
        row["returns"] += source["net"]
        row["net_sales"] -= source["net"]
        row["cost"] -= source["cost"]

    result = [_profit_row(row) for row in rows.values()]
    totals = _profit_totals(result)
    for row in result:
        row["gross_sales"] = quantize_money(row["gross_sales"])
        row["returns"] = quantize_money(row["returns"])
        row["share"] = _percent(row["net_sales"], totals["net_sales"])
    result.sort(key=lambda row: (-row["net_sales"], row["category"]))
    totals["gross_sales"] = quantize_money(sum((row["gross_sales"] for row in result), ZERO))
    totals["returns"] = quantize_money(sum((row["returns"] for row in result), ZERO))

    return Report(
        columns=[
            Column("category", "Category"),
            Column("products", "Products", INT),
            Column("gross_sales", "Sales", MONEY),
            Column("returns", "Returns", MONEY),
            Column("net_sales", "Net Sales", MONEY),
            Column("share", "Share", PERCENT_KIND),
            Column("cost", "Cost", MONEY),
            Column("gross_profit", "Gross Profit", MONEY),
            Column("margin", "Margin", PERCENT_KIND),
        ],
        rows=result,
        totals=totals,
        cards=_profit_cards(totals, _card("Categories", len(result), INT, "slate", "Categories with sales or returns")),
    )


def build_sales_by_customer(start, end, params):
    billed = (
        Sale.objects.filter(range_q(start, end))
        .values("customer_id", "customer__name")
        .annotate(
            invoices=Count("id"),
            total=_money_sum("total_amount"),
            tax=_money_sum("tax_amount"),
            paid=_money_sum("paid_amount"),
            due=_money_sum(_sale_due_expression()),
        )
        .order_by()
    )
    costs = dict(
        SaleItem.objects.filter(range_q(start, end, "sale__created_at"))
        .values("sale__customer_id")
        .annotate(cost=_money_sum(_item_cost_expression()))
        .order_by()
        .values_list("sale__customer_id", "cost")
    )
    returned = (
        SaleReturn.objects.filter(range_q(start, end))
        .values("sale__customer_id", "sale__customer__name")
        .annotate(
            net=_money_sum("net_amount"),
            total=_money_sum("total_amount"),
            cost=_money_sum("cost_amount"),
            receivable=_money_sum("total_amount", filter=Q(refund_method=SaleReturn.RECEIVABLE)),
        )
        .order_by()
    )

    rows = {}

    def row_for(customer_id, name):
        return rows.setdefault(
            customer_id,
            {
                "customer": name or "Walk-in Customer",
                "customer_url": reverse("customer_detail", args=[customer_id]) if customer_id else "",
                "invoices": 0,
                "billed": ZERO,
                "returns": ZERO,
                "net_sales": ZERO,
                "cost": ZERO,
                "paid": ZERO,
                "due": ZERO,
            },
        )

    for source in billed:
        row = row_for(source["customer_id"], source["customer__name"])
        row["invoices"] = source["invoices"]
        row["billed"] += source["total"]
        row["net_sales"] += source["total"] - source["tax"]
        row["cost"] += costs.get(source["customer_id"], ZERO)
        row["paid"] += source["paid"]
        row["due"] += source["due"]
    for source in returned:
        row = row_for(source["sale__customer_id"], source["sale__customer__name"])
        row["returns"] += source["total"]
        row["net_sales"] -= source["net"]
        row["cost"] -= source["cost"]
        row["due"] = max(row["due"] - source["receivable"], ZERO)

    result = []
    for row in rows.values():
        for key in ("billed", "returns", "paid", "due"):
            row[key] = quantize_money(row[key])
        result.append(_profit_row(row))
    result.sort(key=lambda row: (-row["net_sales"], row["customer"]))

    totals = _profit_totals(result)
    for key in ("invoices", "billed", "returns", "paid", "due"):
        totals[key] = sum((row[key] for row in result), 0 if key == "invoices" else ZERO)
    return Report(
        columns=[
            Column("customer", "Customer", url_key="customer_url"),
            Column("invoices", "Invoices", INT),
            Column("billed", "Billed", MONEY),
            Column("returns", "Returns", MONEY),
            Column("net_sales", "Net Sales", MONEY),
            Column("gross_profit", "Gross Profit", MONEY),
            Column("paid", "Paid", MONEY),
            Column("due", "Due on Invoices", MONEY),
        ],
        rows=result,
        totals=totals,
        cards=_profit_cards(totals, _card("Customers", len(result), INT, "slate", "Walk-in sales are grouped together")),
        note="Billed and Returns include tax. Net Sales excludes tax. Open a customer to see the running ledger balance.",
    )


PAYMENT_METHOD_LABELS = {
    "cash": "Cash",
    "bank": "Bank",
    "credit": "Credit (on account)",
}


def build_sales_by_payment_method(start, end, params):
    method = Case(When(is_credit=True, then=Value("credit")), default=F("payment_method"), output_field=CharField())
    billed = (
        Sale.objects.filter(range_q(start, end))
        .annotate(method=method)
        .values("method")
        .annotate(
            invoices=Count("id"),
            total=_money_sum("total_amount"),
            paid=_money_sum("paid_amount"),
            due=_money_sum(_sale_due_expression()),
        )
        .order_by()
    )
    refund_method = Case(
        When(refund_method=SaleReturn.RECEIVABLE, then=Value("credit")),
        default=F("refund_method"),
        output_field=CharField(),
    )
    returned = (
        SaleReturn.objects.filter(range_q(start, end))
        .annotate(method=refund_method)
        .values("method")
        .annotate(count=Count("id"), total=_money_sum("total_amount"))
        .order_by()
    )

    rows = {
        key: {"method": label, "invoices": 0, "billed": ZERO, "collected": ZERO, "due": ZERO, "returns": 0, "refunded": ZERO}
        for key, label in PAYMENT_METHOD_LABELS.items()
    }
    for source in billed:
        row = rows.setdefault(
            source["method"],
            {"method": str(source["method"]).title(), "invoices": 0, "billed": ZERO, "collected": ZERO, "due": ZERO, "returns": 0, "refunded": ZERO},
        )
        row["invoices"] += source["invoices"]
        row["billed"] += source["total"]
        row["collected"] += source["paid"]
        row["due"] += source["due"]
    for source in returned:
        row = rows.setdefault(
            source["method"],
            {"method": str(source["method"]).title(), "invoices": 0, "billed": ZERO, "collected": ZERO, "due": ZERO, "returns": 0, "refunded": ZERO},
        )
        row["returns"] += source["count"]
        row["refunded"] += source["total"]

    result = []
    for key, row in rows.items():
        for money_key in ("billed", "collected", "due", "refunded"):
            row[money_key] = quantize_money(row[money_key])
        # A credit "refund" is a receivable write-down, not money leaving the till.
        row["net_collected"] = row["collected"] if key == "credit" else quantize_money(row["collected"] - row["refunded"])
        result.append(row)

    totals = {
        key: sum((row[key] for row in result), 0 if key in ("invoices", "returns") else ZERO)
        for key in ("invoices", "billed", "collected", "due", "returns", "refunded", "net_collected")
    }
    return Report(
        columns=[
            Column("method", "Payment Method"),
            Column("invoices", "Invoices", INT),
            Column("billed", "Billed", MONEY),
            Column("collected", "Collected", MONEY),
            Column("due", "Due on Invoices", MONEY),
            Column("returns", "Returns", INT),
            Column("refunded", "Returned Value", MONEY),
            Column("net_collected", "Net Collected", MONEY),
        ],
        rows=result,
        totals=totals,
        cards=[
            _card("Invoices", totals["invoices"], INT, "slate"),
            _card("Billed", totals["billed"], accent="primary", meta="Invoice totals including tax"),
            _card("Net Collected", totals["net_collected"], accent="success", meta="Collected less cash and bank refunds"),
            _card("Due on Invoices", totals["due"], accent="danger", meta="Unpaid part of the invoices in this period"),
        ],
        note="For Credit, the returned value reduced what the customer owes instead of paying money back.",
    )


def build_tax_summary(start, end, params):
    settings_label = get_system_settings().tax_label
    sale_rate = Case(
        When(sale__tax_mode=Sale.TAX_MODE_MANUAL, then=F("sale__tax_rate")),
        When(sale__payment_method=Sale.BANK, then=F("bank_tax_rate_snapshot")),
        default=F("cash_tax_rate_snapshot"),
        output_field=MONEY_FIELD,
    )
    sold = (
        SaleItem.objects.filter(range_q(start, end, "sale__created_at"))
        .annotate(rate=sale_rate)
        .values("sale__tax_label", "rate")
        .annotate(taxable=_money_sum(_item_net_expression()))
        .order_by()
    )
    return_rate = Case(
        When(sale_return__sale__tax_mode=Sale.TAX_MODE_MANUAL, then=F("sale_return__sale__tax_rate")),
        When(sale_return__sale__payment_method=Sale.BANK, then=F("sale_item__bank_tax_rate_snapshot")),
        default=F("sale_item__cash_tax_rate_snapshot"),
        output_field=MONEY_FIELD,
    )
    returned = (
        SaleReturnItem.objects.filter(range_q(start, end, "sale_return__created_at"))
        .annotate(rate=return_rate)
        .values("sale_return__sale__tax_label", "rate")
        .annotate(taxable=_money_sum("net_amount"), tax=_money_sum("tax_amount"))
        .order_by()
    )

    rows = {}

    def row_for(label, rate):
        rate = quantize_money(rate or 0)
        label = (label or settings_label or "").strip()
        return rows.setdefault(
            (label, rate),
            {"label": label, "rate": rate, "taxable_sales": ZERO, "tax_on_sales": ZERO, "taxable_returns": ZERO, "tax_on_returns": ZERO},
        )

    for source in sold:
        row = row_for(source["sale__tax_label"], source["rate"])
        row["taxable_sales"] += Decimal(source["taxable"])
    for source in returned:
        row = row_for(source["sale_return__sale__tax_label"], source["rate"])
        row["taxable_returns"] += Decimal(source["taxable"])
        row["tax_on_returns"] += Decimal(source["tax"])

    result = []
    for row in rows.values():
        row["taxable_sales"] = quantize_money(row["taxable_sales"])
        row["tax_on_sales"] = quantize_money(row["taxable_sales"] * row["rate"] * PERCENT)
        row["taxable_returns"] = quantize_money(row["taxable_returns"])
        row["tax_on_returns"] = quantize_money(row["tax_on_returns"])
        result.append(row)
    result.sort(key=lambda row: (row["label"], row["rate"]))

    # Invoices round tax once per invoice, so the per-rate figures can drift by a few
    # cents. The difference is shown on its own line so the total ties to the ledger.
    period = sales_totals(start, end)
    booked_sales_tax = quantize_money(period["tax"] + period["returns_tax"])
    computed_sales_tax = sum((row["tax_on_sales"] for row in result), ZERO)
    rounding = quantize_money(booked_sales_tax - computed_sales_tax)
    if rounding:
        result.append(
            {
                "label": "Invoice rounding",
                "rate": None,
                "taxable_sales": ZERO,
                "tax_on_sales": rounding,
                "taxable_returns": ZERO,
                "tax_on_returns": ZERO,
            }
        )

    for row in result:
        row["net_taxable"] = quantize_money(row["taxable_sales"] - row["taxable_returns"])
        row["net_tax"] = quantize_money(row["tax_on_sales"] - row["tax_on_returns"])

    totals = {
        key: quantize_money(sum((row[key] for row in result), ZERO))
        for key in ("taxable_sales", "tax_on_sales", "taxable_returns", "tax_on_returns", "net_taxable", "net_tax")
    }
    return Report(
        columns=[
            Column("label", "Tax"),
            Column("rate", "Rate", PERCENT_KIND),
            Column("taxable_sales", "Taxable Sales", MONEY),
            Column("tax_on_sales", "Tax on Sales", MONEY),
            Column("taxable_returns", "Taxable Returns", MONEY),
            Column("tax_on_returns", "Tax on Returns", MONEY),
            Column("net_taxable", "Net Taxable", MONEY),
            Column("net_tax", "Net Tax", MONEY),
        ],
        rows=result,
        totals=totals,
        cards=[
            _card("Net Taxable Sales", totals["net_taxable"], accent="primary", meta="Sales before tax, less returns"),
            _card("Tax on Sales", totals["tax_on_sales"], accent="info", meta="Charged on invoices in this period"),
            _card("Tax on Returns", totals["tax_on_returns"], accent="warning", meta="Given back on returns"),
            _card("Net Tax Collected", totals["net_tax"], accent="success", meta="What this period adds to tax payable"),
        ],
        note="The 0% line is sales made without tax. Net Tax matches the tax posted to the ledger for the period.",
    )


# --- Stock -----------------------------------------------------------------

@dataclass(frozen=True)
class _MovementSource:
    """One kind of record that moves a product's shelf stock.

    ``when`` is the record's timestamp, or a date when ``dated`` (a stock return is booked on
    the date typed on it, the same date its voucher carries); a dated movement counts from
    the local midnight that starts its date. ``sign`` is +1 for stock in, -1 for stock out and
    0 for a figure that is only shown (free goods are already inside "sold").
    """

    bucket: str
    model: object
    when: str
    quantity: object
    sign: int
    condition: Q = field(default_factory=Q)
    dated: bool = False


# Buckets of the opening and closing stock report. Stock in less stock out is the net change.
STOCK_IN_BUCKETS = ("purchased", "returned_in", "adjusted_in")
STOCK_OUT_BUCKETS = ("sold", "returned_out", "adjusted_out")


def _stock_movement_sources():
    """Every record that changes Product.stock, in one list so that rolling stock back to a
    date, the stock card and the opening and closing report can never disagree.

    Damaged goods on a stock return never touch saleable stock (they are held for a claim),
    so only good ("saleable") lines count.
    """
    from distribution.models import StockReturn, StockReturnItem

    base = _base_qty_expression()
    saleable = Q(condition=StockReturnItem.SALEABLE)
    return (
        _MovementSource("purchased", PurchaseItem, "purchase__created_at", base, 1),
        _MovementSource("sold", SaleItem, "sale__created_at", base, -1),
        _MovementSource("free", SaleItem, "sale__created_at", base, 0, Q(is_free=True)),
        _MovementSource("returned_in", SaleReturnItem, "sale_return__created_at", base, 1),
        _MovementSource(
            "returned_in", StockReturnItem, "stock_return__return_date", base, 1,
            saleable & Q(stock_return__kind=StockReturn.FROM_CUSTOMER), dated=True,
        ),
        _MovementSource("returned_out", PurchaseReturnItem, "purchase_return__created_at", base, -1),
        _MovementSource(
            "returned_out", StockReturnItem, "stock_return__return_date", base, -1,
            saleable & Q(stock_return__kind=StockReturn.TO_SUPPLIER), dated=True,
        ),
        _MovementSource("adjusted_in", StockAdjustment, "created_at", F("quantity_delta"), 1, Q(quantity_delta__gt=0)),
        _MovementSource(
            "adjusted_out", StockAdjustment, "created_at",
            ExpressionWrapper(Value(ZERO) - F("quantity_delta"), output_field=QTY_FIELD), -1, Q(quantity_delta__lt=0),
        ),
    )


def _first_local_date_from(moment):
    """The first local date whose midnight is at or after ``moment``: where a dated movement starts to count."""
    local = timezone.localtime(moment)
    return local.date() if local.time() == time.min else local.date() + timedelta(days=1)


def _source_queryset(source, lower=None, upper=None, product_ids=None):
    """The source's records in [lower, upper); either bound may be None."""
    queryset = source.model.objects.filter(source.condition)
    if lower is not None:
        value = _first_local_date_from(lower) if source.dated else lower
        queryset = queryset.filter(**{f"{source.when}__gte": value})
    if upper is not None:
        value = _first_local_date_from(upper) if source.dated else upper
        queryset = queryset.filter(**{f"{source.when}__lt": value})
    if product_ids is not None:
        queryset = queryset.filter(product_id__in=product_ids)
    return queryset


def _movement_sums_after(moment, product_ids=None):
    """{product_id: net quantity moved} for every stock movement at or after ``moment``."""
    moved = {}
    for source in _stock_movement_sources():
        if not source.sign:
            continue
        queryset = _source_queryset(source, lower=moment, product_ids=product_ids)
        for product_id, quantity in queryset.values("product_id").annotate(total=_qty_sum(source.quantity)).order_by().values_list("product_id", "total"):
            moved[product_id] = moved.get(product_id, ZERO) + source.sign * Decimal(quantity or 0)
    return moved


def _movement_buckets(lower, upper, product_ids=None, *, by_day=False):
    """Stock moved in [lower, upper), per product and bucket, in base units (all positive).

    Returns {product_id: {bucket: qty}}, or {(product_id, local date): {bucket: qty}} by day.
    """
    tz = timezone.get_current_timezone()
    moved = {}
    for source in _stock_movement_sources():
        queryset = _source_queryset(source, lower, upper, product_ids)
        if by_day:
            day = F(source.when) if source.dated else TruncDate(source.when, tzinfo=tz)
            grouped = queryset.annotate(movement_day=day).values("product_id", "movement_day")
            fields = ("product_id", "movement_day", "total")
        else:
            grouped = queryset.values("product_id")
            fields = ("product_id", "total")
        for values in grouped.annotate(total=_qty_sum(source.quantity)).order_by().values_list(*fields):
            key = (values[0], values[1]) if by_day else values[0]
            buckets = moved.setdefault(key, {})
            buckets[source.bucket] = buckets.get(source.bucket, ZERO) + Decimal(values[-1] or 0)
    return moved


def _net_moved(buckets):
    return sum((buckets.get(name, ZERO) for name in STOCK_IN_BUCKETS), ZERO) - sum(
        (buckets.get(name, ZERO) for name in STOCK_OUT_BUCKETS), ZERO
    )


def build_stock_valuation(start, end, params):
    """Quantity on hand x average cost. ``end`` is the as-of date; ``start`` is ignored."""
    today = timezone.localdate()
    as_of = end or today
    category_id = (params.get("category") or "").strip()

    products = Product.objects.filter(is_active=True, track_stock=True).select_related("category").order_by("name")
    if category_id.isdigit():
        products = products.filter(category_id=int(category_id))

    moved_after = {}
    if as_of < today:
        # Roll today's stock back by everything that moved after the as-of date.
        _, moment = local_day_bounds(None, as_of)
        moved_after = _movement_sums_after(moment)

    rows = []
    for product in products:
        quantity = quantize_qty(Decimal(product.stock or 0) - moved_after.get(product.id, ZERO))
        if quantity == 0:
            continue
        cost_value = quantize_money(quantity * Decimal(product.cost_price or 0))
        retail_value = quantize_money(quantity * Decimal(product.price or 0))
        rows.append(
            {
                "product": product.display_name,
                "sku": product.sku,
                "product_url": date_range_url("report_stock_card", None, None, product=product.id),
                "category": product.category.name if product.category else "Uncategorised",
                "quantity": quantity,
                "unit": product.unit,
                "packer": pack_formatter(product),
                "per_pack": f"per {product.top_pack_label}" if product.has_packaging else "",
                "average_cost": product.top_pack_cost,
                "cost_value": cost_value,
                "sale_price": product.top_pack_price,
                "retail_value": retail_value,
                "potential_profit": quantize_money(retail_value - cost_value),
            }
        )

    totals = {
        key: quantize_money(sum((row[key] for row in rows), ZERO))
        for key in ("cost_value", "retail_value", "potential_profit")
    }
    note = "Valued at each product's current weighted-average cost."
    if as_of < today:
        note += f" Quantities are rolled back to the end of {as_of.strftime('%d %b %Y')}; the cost used is today's average."
    return Report(
        columns=[
            Column("product", "Product", url_key="product_url", note_key="sku"),
            Column("category", "Category"),
            Column("quantity", "On Hand", QTY, unit_key="unit", packs_key="packer"),
            Column("average_cost", "Avg Cost", MONEY, note_key="per_pack"),
            Column("cost_value", "Stock Value", MONEY),
            Column("sale_price", "Sale Price", MONEY, note_key="per_pack"),
            Column("retail_value", "Retail Value", MONEY),
            Column("potential_profit", "Potential Profit", MONEY),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Products in Stock", len(rows), INT, "slate", f"As of {as_of.strftime('%d %b %Y')}"),
            _card("Stock Value", totals["cost_value"], accent="primary", meta="Quantity x average cost"),
            _card("Retail Value", totals["retail_value"], accent="info", meta="Quantity x sale price"),
            _card("Potential Profit", totals["potential_profit"], accent="success", meta="If everything sells at list price"),
        ],
        filters=[
            _select_filter(
                "category",
                "Category",
                category_id,
                [("", "All categories")] + [(category.id, category.name) for category in Category.objects.order_by("name")],
            )
        ],
        note=note,
        empty_message="No stock on hand for this selection.",
    )


def _rate_note(item):
    """Words the unit beside a rate that is not for the base unit, such as per Ctn."""
    return f"per {item.unit_label}" if item.unit_level != PACK_PIECE else ""


def _stock_movements(product, lower, upper):
    def bounded(queryset, date_field):
        if lower is not None:
            queryset = queryset.filter(**{f"{date_field}__gte": lower})
        if upper is not None:
            queryset = queryset.filter(**{f"{date_field}__lt": upper})
        return queryset

    movements = []
    purchases = bounded(PurchaseItem.objects.filter(product=product), "purchase__created_at").select_related("purchase__supplier")
    for item in purchases:
        movements.append(
            {
                "when": item.purchase.created_at,
                "type": "Purchase",
                "reference": f"Purchase #{item.purchase_id}",
                "reference_url": reverse("purchase_detail", args=[item.purchase_id]),
                "party": item.purchase.supplier.name if item.purchase.supplier else "Cash purchase",
                "delta": Decimal(item.base_quantity),
                "rate": item.cost_price,
                "rate_note": _rate_note(item),
            }
        )
    sales = bounded(SaleItem.objects.filter(product=product), "sale__created_at").select_related("sale__customer")
    for item in sales:
        movements.append(
            {
                "when": item.sale.created_at,
                "type": "Sale",
                "reference": f"Invoice {sale_number(item.sale)}",
                "reference_url": reverse("invoice", args=[item.sale_id]),
                "party": item.sale.customer.name if item.sale.customer else "Walk-in customer",
                "delta": -Decimal(item.base_quantity),
                "rate": item.discounted_unit_price,
                "rate_note": _rate_note(item),
            }
        )
    sale_returns = bounded(SaleReturnItem.objects.filter(product=product), "sale_return__created_at").select_related(
        "sale_return__customer", "sale_return__sale"
    )
    for item in sale_returns:
        movements.append(
            {
                "when": item.sale_return.created_at,
                "type": "Sale return",
                "reference": f"Return #{item.sale_return_id} of invoice {sale_number(item.sale_return.sale)}",
                "reference_url": reverse("invoice", args=[item.sale_return.sale_id]),
                "party": item.sale_return.customer.name if item.sale_return.customer else "Walk-in customer",
                "delta": Decimal(item.base_quantity),
                "rate": item.unit_price,
                "rate_note": _rate_note(item),
            }
        )
    purchase_returns = bounded(
        PurchaseReturnItem.objects.filter(product=product), "purchase_return__created_at"
    ).select_related("purchase_return__supplier", "purchase_item")
    for item in purchase_returns:
        movements.append(
            {
                "when": item.purchase_return.created_at,
                "type": "Purchase return",
                "reference": f"Return #{item.purchase_return_id} of purchase #{item.purchase_return.purchase_id}",
                "reference_url": reverse("purchase_detail", args=[item.purchase_return.purchase_id]),
                "party": item.purchase_return.supplier.name if item.purchase_return.supplier else "Cash purchase",
                "delta": -Decimal(item.base_quantity),
                "rate": item.purchase_item.cost_price,
                "rate_note": _rate_note(item.purchase_item),
            }
        )
    for source in _stock_movement_sources():
        if not source.dated:
            continue
        # Good stock on a market return or a return to the supplier (damaged goods never reach the shelf).
        items = _source_queryset(source, lower, upper, [product.pk]).select_related(
            "stock_return__customer", "stock_return__supplier"
        )
        for item in items:
            stock_return = item.stock_return
            created = timezone.localtime(stock_return.created_at)
            when = (
                stock_return.created_at
                if created.date() == stock_return.return_date
                else timezone.make_aware(datetime.combine(stock_return.return_date, time.min), timezone.get_current_timezone())
            )
            movements.append(
                {
                    "when": when,
                    "type": "Market return" if source.sign > 0 else "Return to supplier",
                    "reference": f"Stock return {stock_return.display_number}",
                    "reference_url": reverse("distribution_return_detail", args=[stock_return.pk]),
                    "party": stock_return.party_name,
                    "delta": source.sign * Decimal(item.base_quantity),
                    "rate": item.rate,
                    "rate_note": _rate_note(item),
                }
            )
    adjustments = bounded(StockAdjustment.objects.filter(product=product), "created_at").select_related("created_by")
    for adjustment in adjustments:
        movements.append(
            {
                "when": adjustment.created_at,
                "type": adjustment.get_adjustment_mode_display(),
                "reference": adjustment.reason,
                "reference_url": "",
                "party": adjustment.created_by.get_username() if adjustment.created_by else "",
                "delta": Decimal(adjustment.quantity_delta),
                "rate": None,
            }
        )
    movements.sort(key=lambda movement: movement["when"])
    return movements


def build_stock_card(start, end, params):
    product_id = (params.get("product") or "").strip()
    product = Product.objects.filter(pk=int(product_id)).first() if product_id.isdigit() else None
    filters = [
        _select_filter(
            "product",
            "Product",
            product.id if product else "",
            [("", "Choose a product")]
            + [
                (row.id, f"{row.display_name} ({row.sku})")
                for row in Product.objects.order_by("name", "variant_label").only("id", "name", "sku", "variant_label", "variant_group_id")
            ],
        )
    ]
    columns = [
        Column("when", "Date", DATETIME),
        Column("type", "Movement"),
        Column("reference", "Reference", url_key="reference_url"),
        Column("party", "Party / By"),
        Column("rate", "Unit Rate", MONEY, note_key="rate_note"),
        Column("qty_in", "In", QTY, unit_key="unit", packs_key="packer"),
        Column("qty_out", "Out", QTY, unit_key="unit", packs_key="packer"),
        Column("balance", "Balance", QTY, unit_key="unit", packs_key="packer"),
    ]
    if product is None:
        return Report(columns=columns, rows=[], filters=filters, empty_message="Choose a product to see its stock movements.")

    lower, upper = local_day_bounds(start, end)
    movements = _stock_movements(product, lower, upper)

    # Anchor on the stock on hand today so the card always ends on the real figure,
    # even if older stock was edited before movements were tracked.
    closing = Decimal(product.stock or 0)
    if upper is not None:
        closing -= _movement_sums_after(upper, [product.id]).get(product.id, ZERO)
    opening = closing - sum((movement["delta"] for movement in movements), ZERO)

    rows = []
    balance = opening
    total_in = ZERO
    total_out = ZERO
    packer = pack_formatter(product)
    for movement in movements:
        delta = movement["delta"]
        balance += delta
        if delta >= 0:
            total_in += delta
        else:
            total_out += -delta
        rows.append(
            {
                "when": movement["when"],
                "type": movement["type"],
                "reference": movement["reference"],
                "reference_url": movement["reference_url"],
                "party": movement["party"],
                "rate": movement["rate"],
                "rate_note": movement.get("rate_note", ""),
                "packer": packer,
                "qty_in": quantize_qty(delta) if delta >= 0 else None,
                "qty_out": quantize_qty(-delta) if delta < 0 else None,
                "balance": quantize_qty(balance),
                "unit": product.unit,
            }
        )

    unit = product.unit
    return Report(
        columns=columns,
        rows=rows,
        totals={
            "qty_in": quantize_qty(total_in),
            "qty_out": quantize_qty(total_out),
            "balance": quantize_qty(closing),
            "unit": unit,
            "packer": packer,
        },
        cards=[
            _card("Opening Balance", f"{format_qty(opening)} {unit}", TEXT, "slate", "Before the first day of the period"),
            _card("Stock In", f"{format_qty(total_in)} {unit}", TEXT, "success", "Purchases, returns from customers, adjustments up"),
            _card("Stock Out", f"{format_qty(total_out)} {unit}", TEXT, "warning", "Sales, returns to suppliers, adjustments down"),
            _card("Closing Balance", f"{format_qty(closing)} {unit}", TEXT, "primary", "At the end of the period"),
        ],
        filters=filters,
        note=f"{product.display_name} ({product.sku}). On hand now: {format_qty(product.stock)} {unit}.",
        empty_message="No stock movements for this product in the period.",
    )


# --- Opening and closing stock ----------------------------------------------

PERIOD_DAY = "day"
PERIOD_MONTH = "month"
PERIOD_YEAR = "year"
STOCK_SHOW_OPTIONS = (("", "Products with stock or movement"), ("all", "All products"))


def _month_end(first):
    return date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])


def _parse_month(value):
    """The first day of a month typed as 2026-11 (or a full date in it); None when unreadable."""
    value = (value or "").strip()
    for fmt in ("%Y-%m", "%Y-%m-%d", "%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def _parse_year(value):
    value = (value or "").strip()
    if value.isdigit() and 1900 <= int(value) <= 9999:
        return int(value)
    return None


def parse_report_period(params, periods, today=None):
    """(period, start, end) for a report that covers one day, one month or one year.

    ``periods`` are the codes the report offers, the first being its default. When the
    period changes, the date already chosen carries over: switching 13 Nov to Monthly
    opens November.
    """
    today = today or timezone.localdate()
    period = params.get("period") if params.get("period") in periods else periods[0]
    day = parse_report_date(params.get("day"))
    month = _parse_month(params.get("month"))
    year = _parse_year(params.get("year"))
    if period == PERIOD_DAY:
        if day is None:
            anchor = month or (date(year, 1, 1) if year else None)
            day = today if anchor is None or (anchor.year, anchor.month) == (today.year, today.month) else anchor
        return period, day, day
    if period == PERIOD_MONTH:
        if month is None:
            if day is not None:
                month = day.replace(day=1)
            elif year is not None and year != today.year:
                month = date(year, 1, 1)
            else:
                month = today.replace(day=1)
        return period, month, _month_end(month)
    if year is None:
        year = (day or month or today).year
    return PERIOD_YEAR, date(year, 1, 1), date(year, 12, 31)


def period_label(start, end):
    if start == end:
        return start.strftime("%a %d %b %Y")
    if start.day == 1 and end == _month_end(start):
        return start.strftime("%B %Y")
    if (start.month, start.day, end.month, end.day) == (1, 1, 12, 31) and start.year == end.year:
        return f"Year {start.year}"
    return range_label(start, end)


def _period_shortcuts(periods, today):
    """Quick links for a period report: today and yesterday, this and last month, this and last year."""
    last_month = today.replace(day=1) - timedelta(days=1)
    choices = {
        PERIOD_DAY: (("Today", today.isoformat()), ("Yesterday", (today - timedelta(days=1)).isoformat())),
        PERIOD_MONTH: (("This month", today.strftime("%Y-%m")), ("Last month", last_month.strftime("%Y-%m"))),
        PERIOD_YEAR: (("This year", str(today.year)), ("Last year", str(today.year - 1))),
    }
    shortcuts = []
    for period in periods:
        for label, value in choices.get(period, ()):
            query = {"period": period, period: value}
            _, start, end = parse_report_period(query, periods, today)
            shortcuts.append({"label": label, "query": query, "start": start, "end": end})
    return shortcuts


def _tracked_products(params):
    """Stock-tracked products narrowed by the category and product filters, and those filters."""
    category_id = (params.get("category") or "").strip()
    product_id = (params.get("product") or "").strip()
    products = Product.objects.filter(track_stock=True).select_related("category").order_by("name", "variant_label", "id")
    narrowed = False
    if category_id.isdigit():
        products = products.filter(category_id=int(category_id))
        narrowed = True
    if product_id.isdigit():
        products = products.filter(pk=int(product_id))
        narrowed = True
    filters = [
        _select_filter(
            "category",
            "Category",
            category_id,
            [("", "All categories")] + [(category.id, category.name) for category in Category.objects.order_by("name")],
        ),
        _select_filter(
            "product",
            "Product",
            product_id,
            [("", "All products")]
            + [
                (row.id, f"{row.display_name} ({row.sku})")
                for row in Product.objects.filter(track_stock=True)
                .order_by("name", "variant_label")
                .only("id", "name", "sku", "variant_label", "variant_group_id")
            ],
        ),
    ]
    return list(products), (category_id if category_id.isdigit() else ""), (product_id if product_id.isdigit() else ""), narrowed, filters


def _stock_at_bounds(products, lower, upper, narrowed):
    """({product_id: stock at ``lower``}, {product_id: {bucket: qty}} moved in [lower, upper)).

    Today's stock is rolled back by every movement from ``upper`` on, then by the period's
    own movements, so opening + movements = closing for every product, exactly.
    """
    product_ids = [product.id for product in products] if narrowed else None
    after = _movement_sums_after(upper, product_ids)
    moved = _movement_buckets(lower, upper, product_ids)
    opening = {}
    for product in products:
        closing = Decimal(product.stock or 0) - after.get(product.id, ZERO)
        opening[product.id] = closing - _net_moved(moved.get(product.id, {}))
    return opening, moved


def _qty_or_none(value):
    value = quantize_qty(value)
    return value if value != 0 else None


def build_opening_closing_stock(start, end, params):
    """Per product: stock at the start of the first day, what came in and went out, stock at the end of the last day."""
    today = timezone.localdate()
    start = start or today
    end = end or start
    show_all = (params.get("show") or "").strip() == "all"
    products, category_id, product_id, narrowed, filters = _tracked_products(params)
    filters.append(_select_filter("show", "Show", "all" if show_all else "", STOCK_SHOW_OPTIONS))

    lower, upper = local_day_bounds(start, end)
    opening_stock, moved = _stock_at_bounds(products, lower, upper, narrowed)

    rows = []
    for product in products:
        buckets = moved.get(product.id, {})
        opening = quantize_qty(opening_stock[product.id])
        closing = quantize_qty(opening_stock[product.id] + _net_moved(buckets))
        has_movement = any(buckets.get(name, ZERO) for name in STOCK_IN_BUCKETS + STOCK_OUT_BUCKETS)
        if not show_all and opening == 0 and closing == 0 and not has_movement:
            continue
        cost = Decimal(product.cost_price or 0)
        packer = pack_formatter(product)
        free = quantize_qty(buckets.get("free", ZERO))
        rows.append(
            {
                "product": product.display_name,
                "sku": product.sku,
                "product_url": date_range_url("report_stock_card", start, end, product=product.id),
                "category": product.category.name if product.category else "Uncategorised",
                "opening": opening,
                "purchased": _qty_or_none(buckets.get("purchased", ZERO)),
                "sold": _qty_or_none(buckets.get("sold", ZERO)),
                "sold_note": f"incl. {packer(free) if packer else f'{format_qty(free)} {product.unit}'} free" if free else "",
                "returned_in": _qty_or_none(buckets.get("returned_in", ZERO)),
                "returned_out": _qty_or_none(buckets.get("returned_out", ZERO)),
                "adjusted": _qty_or_none(buckets.get("adjusted_in", ZERO) - buckets.get("adjusted_out", ZERO)),
                "closing": closing,
                "unit": product.unit,
                "packer": packer,
                "opening_value": quantize_money(opening * cost),
                "closing_value": quantize_money(closing * cost),
            }
        )

    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("opening_value", "closing_value")}
    if product_id and len(rows) == 1:
        # One product: its quantities add up too.
        row = rows[0]
        totals.update({key: row[key] for key in ("opening", "purchased", "sold", "returned_in", "returned_out", "adjusted", "closing", "unit", "packer")})
    change = quantize_money(totals["closing_value"] - totals["opening_value"])

    label = period_label(start, end)
    first_day = start.strftime("%d %b %Y")
    last_day = end.strftime("%d %b %Y")
    return Report(
        columns=[
            Column("product", "Product", url_key="product_url", note_key="sku"),
            Column("category", "Category"),
            Column("opening", "Opening Stock", QTY, unit_key="unit", packs_key="packer"),
            Column("purchased", "Purchased", QTY, unit_key="unit", packs_key="packer"),
            Column("sold", "Sold", QTY, unit_key="unit", packs_key="packer", note_key="sold_note"),
            Column("returned_in", "Returned In", QTY, unit_key="unit", packs_key="packer"),
            Column("returned_out", "Returned Out", QTY, unit_key="unit", packs_key="packer"),
            Column("adjusted", "Adjusted +/-", QTY, unit_key="unit", packs_key="packer"),
            Column("closing", "Closing Stock", QTY, unit_key="unit", packs_key="packer"),
            Column("opening_value", "Opening Value", MONEY),
            Column("closing_value", "Closing Value", MONEY),
        ],
        rows=rows,
        totals=totals,
        cards=[
            _card("Opening Value", totals["opening_value"], accent="slate", meta=f"Start of {first_day}"),
            _card("Closing Value", totals["closing_value"], accent="primary", meta=f"End of {last_day}"),
            _card(
                "Net Change", change, accent="success" if change >= 0 else "danger",
                meta="Stock grew in value" if change > 0 else ("Stock fell in value" if change < 0 else "No change in value"),
            ),
            _card("Products", len(rows), INT, "info", label),
        ],
        filters=filters,
        note=(
            f"Opening is the stock at the start of {first_day}, closing at the end of {last_day}. "
            "Sold includes free goods; returned in is goods back on the shelf from customers (sale and market "
            "returns; damaged goods held for a claim are not stock); returned out is goods sent back to suppliers. "
            "Values use each product's current weighted-average cost, not the cost on those dates."
        ),
        empty_message="No product had stock or movement in this period.",
    )


def build_stock_by_period(start, end, params):
    """One line per day of a month (or per month of a year): opening value, stock in, stock out, closing value."""
    today = timezone.localdate()
    start = start or today.replace(day=1)
    end = end or _month_end(start)
    by_month = (end - start).days > 31
    last = min(end, today)
    products, category_id, product_id, narrowed, filters = _tracked_products(params)
    columns = [
        Column("period", "Month" if by_month else "Day", url_key="period_url"),
        Column("opening_value", "Opening Value", MONEY),
        Column("stock_in", "Stock In", MONEY),
        Column("stock_out", "Stock Out", MONEY),
        Column("closing_value", "Closing Value", MONEY),
        Column("change", "Net Change", MONEY),
    ]
    note = (
        "Stock in is purchases, goods back on the shelf from customers and adjustments up; stock out is sales "
        "with free goods, returns to suppliers and adjustments down. Values use each product's current "
        "weighted-average cost. Open a line to see every product."
    )
    if last < start:
        return Report(columns=columns, rows=[], filters=filters, note=note, empty_message="This period has not started yet.")

    lower, _ = local_day_bounds(start, None)
    _, upper = local_day_bounds(None, last)
    product_ids = [product.id for product in products] if narrowed else None
    after = _movement_sums_after(upper, product_ids)
    moved_by_day = _movement_buckets(lower, upper, product_ids, by_day=True)
    cost = {product.id: Decimal(product.cost_price or 0) for product in products}

    # Group each product's movements by the line they fall in: the day, or the first of its month.
    per_line = {}
    period_net = {}
    for (pid, day), buckets in moved_by_day.items():
        if pid not in cost:
            continue
        if isinstance(day, datetime):
            day = day.date()
        key = max(day.replace(day=1), start) if by_month else day
        line = per_line.setdefault(key, {}).setdefault(pid, {"in": ZERO, "out": ZERO})
        line["in"] += sum((buckets.get(name, ZERO) for name in STOCK_IN_BUCKETS), ZERO)
        line["out"] += sum((buckets.get(name, ZERO) for name in STOCK_OUT_BUCKETS), ZERO)
        period_net[pid] = period_net.get(pid, ZERO) + _net_moved(buckets)

    # Stock of each product at the start of the first line.
    on_hand = {}
    for product in products:
        quantity = Decimal(product.stock or 0) - after.get(product.id, ZERO) - period_net.get(product.id, ZERO)
        if quantity:
            on_hand[product.id] = quantity

    def value_of(quantities):
        return sum((quantize_money(quantity * cost[pid]) for pid, quantity in quantities.items()), ZERO)

    keys = []
    cursor = start
    while cursor <= last:
        keys.append(cursor)
        cursor = (_month_end(cursor) + timedelta(days=1)) if by_month else cursor + timedelta(days=1)

    base_params = {"category": category_id, "product": product_id}
    report_path = reverse("report_opening_closing_stock")
    rows = []
    for key in keys:
        opening_value = value_of(on_hand)
        lines = per_line.get(key, {})
        stock_in = ZERO
        for pid, line in lines.items():
            stock_in += quantize_money(line["in"] * cost[pid])
            quantity = on_hand.get(pid, ZERO) + line["in"] - line["out"]
            if quantity:
                on_hand[pid] = quantity
            else:
                on_hand.pop(pid, None)
        closing_value = value_of(on_hand)
        # Out is what closes the line, so each line reads opening + in - out = closing to the paisa.
        stock_out = opening_value + stock_in - closing_value
        if by_month:
            query = {"period": PERIOD_MONTH, "month": key.strftime("%Y-%m")}
            label = key.strftime("%B %Y")
        else:
            query = {"period": PERIOD_DAY, "day": key.isoformat()}
            label = key.strftime("%a %d %b")
        query.update({name: value for name, value in base_params.items() if value})
        rows.append(
            {
                "period": label,
                "period_url": f"{report_path}?{urlencode(query)}",
                "opening_value": quantize_money(opening_value),
                "stock_in": quantize_money(stock_in),
                "stock_out": quantize_money(stock_out),
                "closing_value": quantize_money(closing_value),
                "change": quantize_money(closing_value - opening_value),
            }
        )

    totals = {
        "opening_value": rows[0]["opening_value"],
        "stock_in": quantize_money(sum((row["stock_in"] for row in rows), ZERO)),
        "stock_out": quantize_money(sum((row["stock_out"] for row in rows), ZERO)),
        "closing_value": rows[-1]["closing_value"],
        "change": quantize_money(rows[-1]["closing_value"] - rows[0]["opening_value"]),
    }
    last_text = last.strftime("%d %b %Y")
    return Report(
        columns=columns,
        rows=rows,
        totals=totals,
        cards=[
            _card("Opening Value", totals["opening_value"], accent="slate", meta=f"Start of {start.strftime('%d %b %Y')}"),
            _card("Stock In", totals["stock_in"], accent="success", meta="At current average cost"),
            _card("Stock Out", totals["stock_out"], accent="warning", meta="At current average cost"),
            _card("Closing Value", totals["closing_value"], accent="primary", meta=f"End of {last_text}"),
        ],
        filters=filters,
        note=note,
        empty_message="Nothing to show for this period.",
    )


RETURN_KIND_OPTIONS = (("", "Sale and purchase returns"), ("sale", "Sale returns only"), ("purchase", "Purchase returns only"))


def build_returns(start, end, params):
    kind = (params.get("kind") or "").strip()
    if kind not in ("sale", "purchase"):
        kind = ""
    rows = []
    if kind in ("", "sale"):
        sale_returns = (
            SaleReturn.objects.filter(range_q(start, end)).select_related("sale", "customer", "created_by").order_by("-created_at")
        )
        for sale_return in sale_returns:
            rows.append(
                {
                    "when": sale_return.created_at,
                    "kind": "Sale return",
                    "reference": f"Return #{sale_return.id} of invoice {sale_number(sale_return.sale)}",
                    "reference_url": reverse("invoice", args=[sale_return.sale_id]),
                    "party": sale_return.customer.name if sale_return.customer else "Walk-in customer",
                    "settled": sale_return.get_refund_method_display(),
                    "reason": sale_return.reason,
                    "recorded_by": sale_return.created_by.get_username() if sale_return.created_by else "",
                    "net": sale_return.net_amount,
                    "tax": sale_return.tax_amount,
                    "total": sale_return.total_amount,
                    "cost": sale_return.cost_amount,
                    "is_sale": True,
                }
            )
    if kind in ("", "purchase"):
        purchase_returns = (
            PurchaseReturn.objects.filter(range_q(start, end))
            .select_related("purchase", "supplier", "created_by")
            .order_by("-created_at")
        )
        for purchase_return in purchase_returns:
            rows.append(
                {
                    "when": purchase_return.created_at,
                    "kind": "Purchase return",
                    "reference": f"Return #{purchase_return.id} of purchase #{purchase_return.purchase_id}",
                    "reference_url": reverse("purchase_detail", args=[purchase_return.purchase_id]),
                    "party": purchase_return.supplier.name if purchase_return.supplier else "Cash purchase",
                    "settled": purchase_return.get_settlement_method_display(),
                    "reason": purchase_return.reason,
                    "recorded_by": purchase_return.created_by.get_username() if purchase_return.created_by else "",
                    "net": purchase_return.total_amount,
                    "tax": ZERO,
                    "total": purchase_return.total_amount,
                    "cost": purchase_return.total_amount,
                    "is_sale": False,
                }
            )
    rows.sort(key=lambda row: row["when"], reverse=True)

    sale_rows = [row for row in rows if row["is_sale"]]
    purchase_rows = [row for row in rows if not row["is_sale"]]
    sale_total = quantize_money(sum((row["total"] for row in sale_rows), ZERO))
    sale_net = quantize_money(sum((row["net"] for row in sale_rows), ZERO))
    sale_cost = quantize_money(sum((row["cost"] for row in sale_rows), ZERO))
    purchase_total = quantize_money(sum((row["total"] for row in purchase_rows), ZERO))
    totals = {
        key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("net", "tax", "total", "cost")
    }
    return Report(
        columns=[
            Column("when", "Date", DATETIME),
            Column("kind", "Type"),
            Column("reference", "Reference", url_key="reference_url"),
            Column("party", "Party"),
            Column("settled", "Settled By"),
            Column("reason", "Reason", note_key="recorded_by"),
            Column("net", "Net", MONEY),
            Column("tax", "Tax", MONEY),
            Column("total", "Total", MONEY),
            Column("cost", "Stock Cost", MONEY),
        ],
        rows=rows,
        # Sale and purchase returns move money in opposite directions, so a combined
        # footer would mean nothing. It is only shown when one kind is listed.
        totals=totals if kind else {},
        cards=[
            _card("Sale Returns", len(sale_rows), INT, "slate", "Goods customers brought back"),
            _card("Returned to Customers", sale_total, accent="danger", meta=f"Net {sale_net} before tax"),
            _card("Profit Given Back", quantize_money(sale_net - sale_cost), accent="warning", meta="Net returned less the cost put back in stock"),
            _card("Returned to Suppliers", purchase_total, accent="info", meta=f"{len(purchase_rows)} purchase returns"),
        ],
        filters=[_select_filter("kind", "Show", kind, RETURN_KIND_OPTIONS)],
    )


def _field_sales_report(name):
    """A builder of pos.services.field_sales_reports, imported on first use (that module imports this one)."""

    def build(start, end, params):
        from pos.services import field_sales_reports

        return getattr(field_sales_reports, name)(start, end, params)

    return build


def _distribution_report(name):
    """A builder of distribution.reports, imported on first use (that module imports this one)."""

    def build(start, end, params):
        from distribution import reports as distribution_reports

        return getattr(distribution_reports, name)(start, end, params)

    return build


def _pricing_report(name):
    """A builder of pricing.reports, imported on first use (that module imports this one)."""

    def build(start, end, params):
        from pricing import reports as pricing_reports

        return getattr(pricing_reports, name)(start, end, params)

    return build


REPORTS = (
    ReportSpec("sales-by-product", "Sales by Product", "What sold, how much came back, and the profit each product made.", "bi-bag-check", "Sales", "report_sales_by_product", build_sales_by_product, accent="primary", short_title="By Product"),
    ReportSpec("sales-by-category", "Sales by Category", "Which departments bring the sales and the margin.", "bi-tags", "Sales", "report_sales_by_category", build_sales_by_category, accent="info", short_title="By Category"),
    ReportSpec("sales-by-customer", "Sales by Customer", "Who buys the most, what they returned and what is unpaid.", "bi-people", "Sales", "report_sales_by_customer", build_sales_by_customer, accent="success", short_title="By Customer"),
    ReportSpec("sales-by-order-booker", "Sales by Order Booker", "Invoices, quantity and sales each order booker brought in, net of returns.", "bi-person-badge", "Sales", "report_sales_by_order_booker", _field_sales_report("build_sales_by_order_booker"), accent="info", short_title="By Order Booker"),
    ReportSpec("receivables-by-area", "Receivables by City and Route", "Customer balances grouped by city, route or order booker, with a subtotal for each group.", "bi-pin-map", "Sales", "report_receivables_by_area", _field_sales_report("build_receivables_by_area"), range_mode="as_of", accent="warning", short_title="Receivables"),
    ReportSpec("sales-by-payment-method", "Sales by Payment Method", "Cash, bank and credit sales with refunds taken off.", "bi-credit-card", "Sales", "report_sales_by_payment_method", build_sales_by_payment_method, accent="slate", short_title="By Payment"),
    ReportSpec("tax-summary", "Tax Summary", "Taxable sales and tax collected for each rate, net of returns.", "bi-percent", "Sales", "report_tax_summary", build_tax_summary, accent="warning", short_title="Tax"),
    ReportSpec("returns", "Returns", "Every sale and purchase return with its reason and value.", "bi-arrow-counterclockwise", "Sales", "report_returns", build_returns, accent="danger", short_title="Returns"),
    ReportSpec("stock-valuation", "Stock Valuation", "Quantity on hand x average cost, with units.", "bi-calculator", "Stock", "report_stock_valuation", build_stock_valuation, range_mode="as_of", accent="primary", short_title="Valuation"),
    ReportSpec("stock-card", "Stock Movement Card", "One product: purchases, sales, returns and adjustments with a running balance.", "bi-card-list", "Stock", "report_stock_card", build_stock_card, accent="info", short_title="Movement Card"),
    ReportSpec("opening-closing-stock", "Opening and Closing Stock", "For a day or a month: each product's opening stock, purchased, sold, returned, adjusted and closing stock, with values.", "bi-box-seam", "Stock", "report_opening_closing_stock", build_opening_closing_stock, range_mode="period", accent="primary", short_title="Opening and Closing", periods=((PERIOD_DAY, "Daily"), (PERIOD_MONTH, "Monthly"))),
    ReportSpec("stock-day-by-day", "Stock Day by Day", "Opening value, stock in, stock out and closing value for each day of a month or each month of a year.", "bi-calendar3-week", "Stock", "report_stock_day_by_day", build_stock_by_period, range_mode="period", accent="info", short_title="Day by Day", periods=((PERIOD_MONTH, "Each day of a month"), (PERIOD_YEAR, "Each month of a year"))),
    ReportSpec("order-booking", "Order Booking Summary", "Orders booked by order booker, route or day: booked, invoiced and short.", "bi-clipboard-check", "Distribution", "report_order_booking", _distribution_report("build_order_booking"), accent="primary", short_title="Order Booking"),
    ReportSpec("load-sheets", "Load Sheet History", "Every load form with its invoices, cartons, bill value, returns and cash.", "bi-truck", "Distribution", "report_load_sheets", _distribution_report("build_load_sheets"), accent="info", short_title="Load Sheets"),
    ReportSpec("delivery-settlement", "Delivery Man Settlement", "Bills taken out, returns, cash expected and handed in, for each delivery man.", "bi-person-check", "Distribution", "report_delivery_settlement", _distribution_report("build_delivery_settlement"), accent="warning", short_title="Settlements"),
    ReportSpec("recovery", "Recovery", "Money recovered from customers by route, by person or by day.", "bi-cash-coin", "Distribution", "report_recovery", _distribution_report("build_recovery"), accent="success", short_title="Recovery"),
    ReportSpec("stock-returns", "Stock Returns", "Market returns and returns to suppliers, line by line, good and damaged.", "bi-box-arrow-in-left", "Distribution", "report_stock_returns", _distribution_report("build_stock_returns"), accent="danger", short_title="Stock Returns"),
    ReportSpec("damaged-claims", "Damaged Stock and Claims", "Damaged goods taken back from the market, sent back to suppliers, and still held for a claim.", "bi-exclamation-octagon", "Distribution", "report_damaged_claims", _distribution_report("build_damaged_claims"), accent="danger", short_title="Damaged and Claims"),
    ReportSpec("scheme-usage", "Scheme Usage", "Free goods at cost and discounts given by schemes, by scheme, customer, route, product or day.", "bi-gift", "Pricing", "report_scheme_usage", _pricing_report("build_scheme_usage"), accent="warning", short_title="Scheme Usage"),
    ReportSpec("price-lists", "Price Lists", "Every trade price on every list beside the product price, cost, margin and MRP.", "bi-card-checklist", "Pricing", "report_price_lists", _pricing_report("build_price_lists"), range_mode="none", accent="info", short_title="Price Lists"),
)
REPORTS_BY_SLUG = {spec.slug: spec for spec in REPORTS}

# Pages that live in their own views but belong in the same switcher.
BUILT_IN_REPORTS = (
    ReportSpec("daily-sales", "Sales by Invoice", "Every invoice with discount, tax, cost, profit, paid and due.", "bi-receipt", "Sales", "daily_sales_report", short_title="By Invoice"),
    ReportSpec("employees", "Employees", "Sales, profit, time and wages for each login.", "bi-person-workspace", "Team", "employee_report_list", short_title="Employees"),
    ReportSpec("low-stock", "Low Stock", "Products at or below their reorder level.", "bi-exclamation-triangle", "Stock", "low_stock_list", range_mode="none", short_title="Low Stock"),
    ReportSpec("expiry", "Expiry Alerts", "Products near expiry or already expired.", "bi-hourglass-split", "Stock", "expiry_alert_report", range_mode="none", short_title="Expiry"),
)


ALL_REPORTS = (
    BUILT_IN_REPORTS[0],
    *REPORTS,
    BUILT_IN_REPORTS[2],
    BUILT_IN_REPORTS[3],
    BUILT_IN_REPORTS[1],
)


def report_url(spec, start=None, end=None):
    """Link to a report that keeps the dates the user is already looking at."""
    if spec.range_mode == "range" and start and end:
        return date_range_url(spec.url_name, start, end)
    if spec.range_mode == "as_of" and end:
        return date_range_url(spec.url_name, None, end)
    return reverse(spec.url_name)


def report_navigation(active_slug="", start=None, end=None):
    """Grouped links for the report switcher. The chosen dates travel with every link."""
    from pos.services.modules import report_enabled

    groups = {}
    for spec in ALL_REPORTS:
        if not report_enabled(spec.slug):
            continue
        groups.setdefault(spec.group, []).append(
            {
                "slug": spec.slug,
                "title": spec.title,
                "short_title": spec.short_title or spec.title,
                "description": spec.description,
                "icon": spec.icon,
                "accent": spec.accent,
                "url": report_url(spec, start, end),
                "active": spec.slug == active_slug,
            }
        )
    return [{"name": name, "links": links} for name, links in groups.items()]


def _as_of_shortcuts(today):
    month_end = today.replace(day=1) - timedelta(days=1)
    return [
        {"label": "Today", "start": None, "end": today},
        {"label": "Yesterday", "start": None, "end": today - timedelta(days=1)},
        {"label": "End of last month", "start": None, "end": month_end},
    ]


def report_shell_context(request, active_slug, *, start=None, end=None, range_mode="range", extra_params=None, periods=()):
    """Context every report page needs: switcher, date shortcuts, export link.

    ``periods`` ((code, label), ...) is for range_mode "period": one day, month or year.
    """
    extra_params = {key: value for key, value in (extra_params or {}).items() if value not in (None, "")}
    shortcuts = []
    if range_mode == "range":
        shortcuts = quick_ranges()
    elif range_mode == "as_of":
        shortcuts = _as_of_shortcuts(timezone.localdate())
    period_context = {}
    if range_mode == "period":
        codes = tuple(code for code, _ in periods)
        period, _, _ = parse_report_period(request.GET, codes)
        period_context = {"period": period, "period_options": list(periods)}
    ranges = []
    if range_mode == "period":
        for item in _period_shortcuts(codes, timezone.localdate()):
            ranges.append(
                {
                    "label": item["label"],
                    "url": f"{request.path}?{urlencode({**extra_params, **item['query']})}",
                    "active": item["start"] == start and item["end"] == end,
                }
            )
    for item in shortcuts:
        query = dict(extra_params)
        if item["start"]:
            query["start"] = item["start"].isoformat()
        query["end"] = item["end"].isoformat()
        ranges.append(
            {
                "label": item["label"],
                "url": f"{request.path}?{urlencode(query)}",
                "active": item["start"] == start and item["end"] == end,
            }
        )
    if range_mode == "range":
        ranges.append({"label": "All time", "url": f"{request.path}?{urlencode({**extra_params, 'all': '1'})}", "active": not start and not end})

    export_query = request.GET.copy()
    export_query.pop("page", None)
    export_query["export"] = "csv"
    page_query = request.GET.copy()
    page_query.pop("page", None)
    page_query.pop("export", None)
    if range_mode == "range":
        label = range_label(start, end)
    elif range_mode == "as_of":
        label = f"As of {(end or timezone.localdate()).strftime('%d %b %Y')}"
    elif range_mode == "period" and start and end:
        label = period_label(start, end)
    else:
        label = ""
    return {
        **period_context,
        "report_groups": report_navigation(active_slug, start, end),
        "report_slug": active_slug,
        "range_mode": range_mode,
        "start": start,
        "end": end,
        "range_label": label,
        "quick_ranges": ranges,
        "extra_params": extra_params,
        "export_url": f"{request.path}?{export_query.urlencode()}",
        "page_querystring": page_query.urlencode(),
    }


# ---------------------------------------------------------------------------
# Table cells and CSV
# ---------------------------------------------------------------------------

class ReportRowCells(list):
    """The cells of one table row. ``kind`` is "" for a plain row, "group" for a heading line
    that names a group ("City : Mirpur A.K") and "subtotal" for the line that closes one.
    A builder marks such a row with ``"_kind"`` in the row dict."""

    kind = ""


def report_table_rows(report, rows=None):
    """Rows as lists of cells the generic template can render without lookups by key."""
    table = []
    for row in report.rows if rows is None else rows:
        cells = ReportRowCells()
        cells.kind = row.get("_kind", "")
        for column in report.columns:
            cells.append(
                {
                    "value": row.get(column.key),
                    "kind": column.kind,
                    "numeric": column.is_numeric,
                    "unit": row.get(column.unit_key) if column.unit_key else "",
                    "url": row.get(column.url_key) if column.url_key else "",
                    "note": row.get(column.note_key) if column.note_key else "",
                    "packs": _packs_text(row, column),
                }
            )
        table.append(cells)
    return table


def _packs_text(row, column):
    """A quantity cell of a packed product reads in cartons, boxes and pieces."""
    packer = row.get(column.packs_key) if column.packs_key else None
    value = row.get(column.key)
    if packer is None or value is None or value == "":
        return ""
    return packer(value)


def pack_formatter(product):
    """The function a report row carries so its quantities read in packs; None for a plain product."""
    return product.format_quantity if product is not None and product.has_packaging else None


def report_total_cells(report):
    if not report.totals:
        return []
    cells = []
    for index, column in enumerate(report.columns):
        value = report.totals.get(column.key)
        if index == 0 and value is None:
            cells.append({"value": "Total", "kind": TEXT, "numeric": False, "unit": "", "url": "", "note": "", "packs": ""})
            continue
        cells.append(
            {
                "value": value,
                "kind": column.kind if value is not None else TEXT,
                "numeric": column.is_numeric,
                "unit": report.totals.get(column.unit_key, "") if column.unit_key else "",
                "url": "",
                "note": "",
                "packs": _packs_text(report.totals, column) if value is not None else "",
            }
        )
    return cells


def _csv_text(value):
    text = "" if value is None else str(value)
    # Stop spreadsheet apps from running a product or customer name as a formula.
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{text}"
    return text


def _csv_value(value, kind):
    if value is None or value == "":
        return ""
    if kind == MONEY or kind == PERCENT_KIND:
        return f"{quantize_money(value):.2f}"
    if kind == QTY:
        return format_qty(value)
    if kind == INT:
        return str(value)
    if kind == DATETIME:
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if hasattr(value, "hour") else str(value)
    return _csv_text(value)


def csv_response(filename, header, rows):
    """A utf-8-sig CSV download: the BOM makes Excel read names in any language correctly."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return response


def report_csv_response(report, filename):
    def line(values, units, header=False):
        """One CSV line. Each unit gets a column of its own after its first quantity."""
        cells = []
        seen_units = set()
        for column in report.columns:
            cells.append(column.label if header else _csv_value(values.get(column.key), column.kind))
            if column.kind == QTY and column.unit_key and column.unit_key not in seen_units:
                seen_units.add(column.unit_key)
                cells.append("Unit" if header else _csv_text(units.get(column.unit_key, "")))
        return cells

    # A group heading only repeats what its rows already carry in a column; subtotal lines are kept.
    lines = [line(row, row) for row in report.rows if row.get("_kind") != "group"]
    if report.totals:
        total_line = line(report.totals, report.totals)
        if not total_line[0]:
            total_line[0] = "Total"
        lines.append(total_line)
    return csv_response(filename, line({}, {}, header=True), lines)


def report_filename(slug, start=None, end=None):
    parts = [slug]
    if start:
        parts.append(start.isoformat())
    if end and end != start:
        parts.append(end.isoformat())
    return "_".join(parts) + ".csv"
