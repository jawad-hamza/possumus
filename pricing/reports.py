"""The pricing reports, built for the report framework of ``pos.services.reporting``.

* Price Lists: every price on every list beside the product's own price, cost and MRP.
* Scheme Usage: what schemes gave away in a period, by scheme, customer, route, product or day:
  free goods at their cost (what left stock) and the money discounts took off bill lines.
  Figures are as invoiced; goods returned later are not taken off.
"""
from decimal import Decimal

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from pos.models import SaleItem, quantize_money, quantize_qty
from pos.services.reporting import INT, MONEY, QTY, Column, Report, _card, _select_filter, pack_formatter, range_q

from .models import PriceList, PriceListItem, Scheme

ZERO = Decimal("0")


def _choice(params, name, options, default):
    value = (params.get(name) or "").strip()
    return value if value in dict(options) else default


# ------------------------------------------------------------------------- price lists
def build_price_lists(start, end, params):
    lists = list(PriceList.objects.order_by("name"))
    options = [("", "Every price list")] + [(str(price_list.pk), price_list.name) for price_list in lists]
    chosen = _choice(params, "list", options, "")
    items = PriceListItem.objects.select_related("price_list", "product", "product__category").order_by(
        "price_list__name", "product__name", "product__variant_label"
    )
    if chosen:
        items = items.filter(price_list_id=chosen)
    rows = []
    below = 0
    for item in items:
        product = item.product
        top = product.top_pack_level
        list_price = item.pack_price(top)
        product_price = product.pack_price(top)
        cost = product.pack_cost(top)
        is_below = list_price < product.minimum_sale_price_for_cost(cost)
        below += 1 if is_below else 0
        rows.append(
            {
                "price_list": item.price_list.name,
                "price_list_url": reverse("pricing_price_list_detail", args=[item.price_list_id]),
                "status": "In use" if item.price_list.is_active else "Switched off",
                "product": product.display_name,
                "sku": product.sku,
                "category": product.category.name if product.category_id else "",
                "unit": product.pack_label(top),
                "product_price": product_price,
                "list_price": list_price,
                "change": quantize_money((list_price - product_price) / product_price * 100) if product_price else None,
                "cost": cost,
                "margin": quantize_money((list_price - cost) / cost * 100) if cost else None,
                "retail_price": product.pack_retail_price(top),
                "check": "Below minimum margin" if is_below else "",
            }
        )
    return Report(
        columns=[
            Column("price_list", "Price list", url_key="price_list_url", note_key="status"),
            Column("product", "Product", note_key="sku"),
            Column("category", "Category"),
            Column("unit", "Per"),
            Column("product_price", "Product price", MONEY),
            Column("list_price", "List price", MONEY),
            Column("change", "Change %", "percent"),
            Column("cost", "Cost", MONEY),
            Column("margin", "Margin %", "percent"),
            Column("retail_price", "MRP", MONEY),
            Column("check", "Check"),
        ],
        rows=rows,
        cards=[
            _card("Price lists", len(lists), INT, "slate", f"{sum(1 for price_list in lists if price_list.is_active)} in use"),
            _card("Prices", len(rows), INT, "primary", "Products on the lists shown"),
            _card("Below minimum margin", below, INT, "danger", "Refused at the till until raised"),
        ],
        filters=[_select_filter("list", "Price list", chosen, options)],
        note="Prices are per carton, or the largest pack each product has. Cost is today's average cost.",
        empty_message="No prices on any list yet.",
    )


# ------------------------------------------------------------------------ scheme usage
USAGE_GROUPS = (
    ("scheme", "By scheme"),
    ("customer", "By customer"),
    ("route", "By route"),
    ("product", "By product given free"),
    ("day", "By day"),
)


def _usage_lines(start, end):
    lines = SaleItem.objects.filter(range_q(start, end, "sale__created_at")).filter(
        Q(is_free=True) | Q(scheme_discount__gt=0)
    )
    return lines.select_related("sale", "sale__customer", "sale__customer__route", "product", "scheme")


def build_scheme_usage(start, end, params):
    by = _choice(params, "by", USAGE_GROUPS, "scheme")
    groups = {}
    for line in _usage_lines(start, end):
        sale = line.sale
        customer = sale.customer
        if by == "scheme":
            key = line.scheme_id or f"name:{line.scheme_name}"
            label = line.scheme.name if line.scheme_id else (line.scheme_name or "Scheme removed")
        elif by == "customer":
            key = customer.pk if customer else 0
            label = customer.name if customer else "Walk-in customer"
        elif by == "route":
            route = customer.route if customer and customer.route_id else None
            key = route.pk if route else 0
            label = route.label if route else "No route"
        elif by == "product":
            if not line.is_free:
                continue
            key = line.product_id
            label = line.product.display_name
        else:
            day = timezone.localtime(sale.created_at).date()
            key = day
            label = day.strftime("%d %b %Y")
        row = groups.setdefault(
            key,
            {
                "group": label,
                "invoices": set(),
                "free_lines": 0,
                "free_base": ZERO,
                "free_cost": ZERO,
                "discount": ZERO,
                "_sort": key if by == "day" else label.casefold(),
                "packer": pack_formatter(line.product) if by == "product" else None,
                "unit": line.product.unit if by == "product" else "",
            },
        )
        row["invoices"].add(sale.pk)
        if line.is_free:
            row["free_lines"] += 1
            row["free_base"] += line.base_quantity
            row["free_cost"] += line.line_cost
        else:
            row["discount"] += Decimal(line.scheme_discount or 0)
    rows = sorted(groups.values(), key=lambda row: row["_sort"], reverse=(by == "day"))
    for row in rows:
        row["invoices"] = len(row["invoices"])
        row["free_cost"] = quantize_money(row["free_cost"])
        row["discount"] = quantize_money(row["discount"])
        row["total"] = quantize_money(row["free_cost"] + row["discount"])
        row["free_base"] = quantize_qty(row["free_base"])
    totals = {key: quantize_money(sum((row[key] for row in rows), ZERO)) for key in ("free_cost", "discount", "total")}
    totals["free_lines"] = sum(row["free_lines"] for row in rows)
    invoice_count = len({line.sale_id for line in _usage_lines(start, end)})
    columns = [
        Column("group", dict(USAGE_GROUPS)[by].replace("By ", "").capitalize()),
        Column("invoices", "Invoices", INT),
        Column("free_lines", "Free lines", INT),
    ]
    if by == "product":
        columns.append(Column("free_base", "Free quantity", QTY, unit_key="unit", packs_key="packer"))
    columns += [
        Column("free_cost", "Free goods at cost", MONEY),
        Column("discount", "Discounts given", MONEY),
        Column("total", "Total given", MONEY),
    ]
    running = Scheme.objects.filter(is_active=True).count()
    return Report(
        columns=columns,
        rows=rows,
        totals=totals,
        cards=[
            _card("Invoices with a scheme", invoice_count, INT, "slate", f"{running} scheme{'s' if running != 1 else ''} switched on"),
            _card("Free goods at cost", totals["free_cost"], accent="warning", meta="What the free goods cost: it is in Cost of Goods Sold"),
            _card("Discounts given", totals["discount"], accent="info", meta="Taken off bill lines by schemes, before the bill discount"),
            _card("Total given", totals["total"], accent="danger", meta="Free goods at cost plus discounts"),
        ],
        filters=[_select_filter("by", "Group", by, USAGE_GROUPS)],
        note="As invoiced: goods returned afterwards are not taken off. A route is the customer's route today.",
        empty_message="No scheme was used in this period.",
    )
