"""Reports home, sales by invoice, low stock, expiry and employee reports."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from accounts.models import Expense
from ..models import Sale, UserSessionLog, quantize_money, quantize_qty
from ..services.access import feature_required, get_user_role_label, module_access_required
from ..services.modules import module_enabled, report_enabled
from ..services.currency import format_money
from ..services.business_rules import (
    expired_products,
    get_system_settings,
    low_stock_products,
    near_expiry_products,
)
from ..services.reporting import (
    INT,
    MONEY,
    QTY,
    REPORTS,
    Column,
    Report,
    annotate_sale_rows,
    business_snapshot,
    csv_response,
    employee_sales_totals,
    employee_session_totals,
    employee_wage_totals,
    empty_sales_totals,
    parse_report_range,
    range_q,
    report_csv_response,
    report_filename,
    report_shell_context,
    report_table_rows,
    report_url,
    sales_totals,
)

SALES_PAGE_SIZE = 50
LOG_PAGE_SIZE = 25


def _session_duration(log, now=None):
    end_time = log.logout_at or now or timezone.now()
    if end_time < log.login_at:
        return timedelta(0)
    return end_time - log.login_at


def _format_duration(delta):
    total_seconds = max(int(delta.total_seconds()), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _employee_rows(users, start=None, end=None, now=None):
    """One row per user from four grouped queries, however many users there are."""
    sales = employee_sales_totals(start, end)
    wages = employee_wage_totals(start, end)
    sessions = employee_session_totals(start, end, now=now)
    no_wages = {"booked": Decimal("0"), "paid": Decimal("0"), "due": Decimal("0"), "count": 0}

    rows = []
    for user in users:
        sales_totals_row = sales.get(user.id) or empty_sales_totals()
        wage_row = wages.get(user.id, no_wages)
        session_row = sessions.get(user.id, {"duration": timedelta(0), "active": 0})
        rows.append(
            {
                "user": user,
                "role_label": get_user_role_label(user),
                "sales_totals": sales_totals_row,
                "sales_count": sales_totals_row["invoices"],
                "total_time": session_row["duration"],
                "total_time_display": _format_duration(session_row["duration"]),
                "active_sessions": session_row["active"],
                "wages_booked": wage_row["booked"],
                "wages_paid": wage_row["paid"],
                "wages_due": wage_row["due"],
                "wage_expense_count": wage_row["count"],
            }
        )
    rows.sort(key=lambda row: (row["sales_totals"]["net_sales"], row["sales_count"]), reverse=True)
    return rows


def _active_users():
    return get_user_model().objects.filter(is_active=True).order_by("first_name", "username")


@module_access_required("reports")
def reports_home(request):
    snapshot = business_snapshot()
    today = snapshot["today"]
    week_start = snapshot["week_start"]
    month_start = snapshot["month_start"]
    settings_obj = snapshot["settings"]
    today_sales = snapshot["today_sales"]
    week_sales = snapshot["week_sales"]
    month_sales = snapshot["month_sales"]
    low_stock_count = snapshot["low_stock_count"]
    near_expiry_count = snapshot["near_expiry_count"]
    expired_count = snapshot["expired_count"]
    expense_due_total = snapshot["expense_due_total"]
    today_report_url = snapshot["today_report_url"]
    week_report_url = snapshot["week_report_url"]
    month_report_url = snapshot["month_report_url"]
    expiry_report_url = snapshot["expiry_report_url"]

    near_expiry_preview = list(snapshot["near_expiry_products"][:8])
    expired_preview = list(snapshot["expired_products"][:5])
    for product in near_expiry_preview:
        product.days_to_expiry = (product.expiry_date - today).days
    for product in expired_preview:
        product.days_overdue = (today - product.expiry_date).days

    employee_rows = _employee_rows(_active_users(), now=timezone.now())
    employee_report_url = reverse("employee_report_list")

    report_kpis = [
        {
            "title": "Today Sales",
            "value": today_sales["net_sales"],
            "meta": f"{today_sales['invoices']} invoices",
            "url": today_report_url,
            "kind": "money",
            "accent": "primary",
            "icon": "bi-calendar-day",
        },
        {
            "title": "Last 7 Days",
            "value": week_sales["net_sales"],
            "meta": f"Gross profit {format_money(week_sales['gross_profit'])}",
            "url": week_report_url,
            "kind": "money",
            "accent": "info",
            "icon": "bi-graph-up",
        },
        {
            "title": "This Month",
            "value": month_sales["net_sales"],
            "meta": f"{month_sales['invoices']} invoices, returns {format_money(month_sales['returns_net'])}",
            "url": month_report_url,
            "kind": "money",
            "accent": "success",
            "icon": "bi-calendar-month",
        },
        {
            "title": "Low Stock",
            "value": low_stock_count,
            "meta": "Products needing restock",
            "url": reverse("low_stock_list"),
            "kind": "number",
            "accent": "danger",
            "icon": "bi-boxes",
        },
        {
            "title": "Expiry Alerts",
            "value": near_expiry_count,
            "meta": "Expiry tracking is off" if not settings_obj.enable_product_expiry else f"{expired_count} expired items",
            "url": expiry_report_url,
            "kind": "number",
            "accent": "warning",
            "icon": "bi-hourglass-split",
        },
    ]
    report_cards = [
        {
            "eyebrow": "Sales by invoice",
            "title": "Daily Sales Report",
            "description": "See each invoice with discounts, tax, collected amount, due amount, cost, and gross profit.",
            "metric_label": "Today gross profit",
            "metric_value": today_sales["gross_profit"],
            "metric_kind": "money",
            "url": today_report_url,
            "cta": "Open daily report",
            "icon": "bi-receipt",
            "accent": "primary",
        },
        {
            "eyebrow": "Inventory watch",
            "title": "Low Stock Report",
            "description": "Find products that are at or below their reorder threshold before you miss sales.",
            "metric_label": "Products",
            "metric_value": low_stock_count,
            "metric_kind": "number",
            "url": reverse("low_stock_list"),
            "cta": "Open low stock",
            "icon": "bi-exclamation-diamond",
            "accent": "danger",
        },
        {
            "eyebrow": "Expiry watch",
            "title": "Near Expiry Report",
            "description": "Catch stock that is about to expire so you can clear or rotate it before it becomes a loss.",
            "metric_label": "Items flagged",
            "metric_value": near_expiry_count + expired_count,
            "metric_kind": "number",
            "url": expiry_report_url,
            "cta": "Open expiry report",
            "icon": "bi-hourglass-split",
            "accent": "warning",
        },
        {
            "eyebrow": "Operating costs",
            "title": "Expense Register",
            "description": "Track rent, utilities, wages, and any other bills with paid and unpaid portions.",
            "metric_label": "Bills due",
            "metric_value": expense_due_total,
            "metric_kind": "money",
            "url": reverse("expense_list"),
            "cta": "Open expenses",
            "icon": "bi-receipt-cutoff",
            "accent": "slate",
        },
        {
            "eyebrow": "Team performance",
            "title": "Employee Report",
            "description": "See session logs, total time, wages, profit, and sales generated by each login.",
            "metric_label": "Active users",
            "metric_value": len(employee_rows),
            "metric_kind": "number",
            "url": employee_report_url,
            "cta": "Open employee reports",
            "icon": "bi-people-fill",
            "accent": "slate",
        },
    ]
    # Cards and figures that belong to a switched-off module disappear with it.
    show_expiry_reports = report_enabled("expiry")
    show_employee_reports = report_enabled("employees")
    if not show_expiry_reports:
        report_kpis = [kpi for kpi in report_kpis if kpi["url"] != expiry_report_url]
        report_cards = [card for card in report_cards if card["url"] != expiry_report_url]
    if not show_employee_reports:
        report_cards = [card for card in report_cards if card["url"] != employee_report_url]
    if not module_enabled("expenses"):
        report_cards = [card for card in report_cards if card["url"] != reverse("expense_list")]

    # The analysis reports open on this month, the same period the figures above use.
    analysis_reports = [
        {
            "title": spec.title,
            "description": spec.description,
            "icon": spec.icon,
            "accent": spec.accent,
            "group": spec.group,
            "url": report_url(spec, month_start, today),
        }
        for spec in REPORTS
        if report_enabled(spec.slug)
    ]

    return render(
        request,
        "pos/reports_home.html",
        {
            "show_expiry_reports": show_expiry_reports,
            "show_employee_reports": show_employee_reports,
            "today": today,
            "week_start": week_start,
            "month_start": month_start,
            "report_kpis": report_kpis,
            "report_cards": report_cards,
            "analysis_reports": analysis_reports,
            "month_sales": month_sales,
            "today_report_url": today_report_url,
            "week_report_url": week_report_url,
            "month_report_url": month_report_url,
            "expiry_report_url": expiry_report_url,
            "employee_report_url": employee_report_url,
            "top_low_stock": snapshot["low_stock_products"][:10],
            "near_expiry_preview": near_expiry_preview,
            "expired_preview": expired_preview,
            "best_selling_month": snapshot["best_selling_month"][:8],
            "employee_rows": employee_rows[:8],
            "month_purchase_total": snapshot["month_purchase_total"],
            "expense_due_total": expense_due_total,
        },
    )


def _expiry_report(near_expiry_list, expired_list, today):
    rows = []
    for status, products in (("Near expiry", near_expiry_list), ("Expired", expired_list)):
        for product in products:
            rows.append(
                {
                    "status": status,
                    "product": product.display_name,
                    "sku": product.sku,
                    "category": product.category.name if product.category else "",
                    "location": product.location,
                    "expiry_date": product.expiry_date.isoformat(),
                    # Negative once the date has passed.
                    "days": (product.expiry_date - today).days,
                    "quantity": product.stock,
                    "unit": product.unit,
                    "stock_value": product.stock_value_at_risk,
                }
            )
    return Report(
        columns=[
            Column("status", "Status"),
            Column("product", "Product"),
            Column("sku", "SKU"),
            Column("category", "Category"),
            Column("location", "Location"),
            Column("expiry_date", "Expiry Date"),
            Column("days", "Days Left", INT),
            Column("quantity", "On Hand", QTY, unit_key="unit"),
            Column("stock_value", "Stock Value", MONEY),
        ],
        rows=rows,
    )


@module_access_required("reports")
@feature_required("expiry", ceiling_only=True)
def expiry_alert_report(request):
    settings_obj = get_system_settings()
    today = timezone.localdate()
    near_expiry_list = list(near_expiry_products(settings_obj, today=today))
    expired_list = list(expired_products(settings_obj, today=today))

    for product in near_expiry_list:
        product.days_to_expiry = (product.expiry_date - today).days
    for product in expired_list:
        product.days_overdue = (today - product.expiry_date).days
    for product in (*near_expiry_list, *expired_list):
        product.stock_value_at_risk = quantize_money(Decimal(product.stock or 0) * Decimal(product.cost_price or 0))

    if request.GET.get("export") == "csv":
        return report_csv_response(_expiry_report(near_expiry_list, expired_list, today), report_filename("expiry-alerts", today))

    context = report_shell_context(request, "expiry", range_mode="none")
    context.update(
        {
            "report_title": "Expiry Alerts",
            "report_description": "Products that are near expiry or already expired, with the stock still on the shelf.",
            "report_icon": "bi-hourglass-split",
            "report_icon_tone": "warning",
            "today": today,
            "near_expiry_products": near_expiry_list,
            "expired_products": expired_list,
            "near_expiry_value": sum((product.stock_value_at_risk for product in near_expiry_list), Decimal("0")),
            "expired_value": sum((product.stock_value_at_risk for product in expired_list), Decimal("0")),
            "expiry_tracking_enabled": settings_obj.enable_product_expiry,
            "expiry_alert_days": settings_obj.expiry_alert_days,
        }
    )
    return render(request, "pos/expiry_alert_report.html", context)


@module_access_required("reports")
@feature_required("team")
def employee_report_list(request):
    # No dates means the whole history, which is what this page always showed.
    start, end = parse_report_range(request.GET)
    employee_rows = _employee_rows(_active_users(), start, end, now=timezone.now())

    if request.GET.get("export") == "csv":
        header = ["User", "Username", "Role", "Invoices", "Net Sales", "Returns", "Gross Profit", "Wages Paid", "Wages Due", "Logged Time"]
        lines = [
            [
                row["user"].get_full_name() or row["user"].username,
                row["user"].username,
                row["role_label"],
                row["sales_count"],
                f"{row['sales_totals']['net_sales']:.2f}",
                f"{row['sales_totals']['returns_net']:.2f}",
                f"{row['sales_totals']['gross_profit']:.2f}",
                f"{row['wages_paid']:.2f}",
                f"{row['wages_due']:.2f}",
                row["total_time_display"],
            ]
            for row in employee_rows
        ]
        return csv_response(report_filename("employees", start, end), header, lines)

    context = report_shell_context(request, "employees", start=start, end=end)
    context.update(
        {
            "report_title": "Employee Reports",
            "report_description": "Session time, sales, profit, and wages linked to each login.",
            "report_icon": "bi-people-fill",
            "report_icon_tone": "slate",
            "employee_rows": employee_rows,
        }
    )
    return render(request, "pos/employee_report_list.html", context)


@module_access_required("reports")
@feature_required("team")
def employee_report_detail(request, user_id):
    employee = get_object_or_404(get_user_model(), pk=user_id)
    start, end = parse_report_range(request.GET)
    now = timezone.now()
    metrics = _employee_rows([employee], start, end, now=now)[0]

    session_page = Paginator(
        UserSessionLog.objects.filter(range_q(start, end, "login_at"), user=employee).order_by("-login_at"),
        LOG_PAGE_SIZE,
    ).get_page(request.GET.get("page"))
    for log in session_page:
        log.duration_display = _format_duration(_session_duration(log, now=now))

    wage_expenses = Expense.objects.filter(employee=employee).select_related("category").order_by("-date", "-id")
    if start:
        wage_expenses = wage_expenses.filter(date__gte=start)
    if end:
        wage_expenses = wage_expenses.filter(date__lte=end)

    recent_sales = annotate_sale_rows(
        Sale.objects.filter(range_q(start, end), sold_by=employee)
        .select_related("customer")
        .prefetch_related("items")
        .order_by("-created_at")[:20]
    )

    context = report_shell_context(request, "employees", start=start, end=end)
    context.update(
        {
            "report_title": employee.get_full_name() or employee.get_username(),
            "report_description": (
                f"Role {metrics['role_label']}. {metrics['sales_count']} sales, "
                f"{metrics['total_time_display']} logged time."
            ),
            "report_icon": "bi-person-workspace",
            "hide_export": True,
            "employee": employee,
            "metrics": metrics,
            "session_logs": session_page,
            "wage_expenses": wage_expenses[:20],
            "recent_sales": recent_sales,
        }
    )
    return render(request, "pos/employee_report_detail.html", context)


def _sales_csv(sales, tax_label, start, end):
    header = [
        "Invoice", "Date", "Customer", "Sold By", "Payment", "Subtotal", "Discount", tax_label or "Tax",
        "Total", "Cost", "Gross Profit", "Paid", "Due", "Returned",
    ]
    lines = []
    for sale in annotate_sale_rows(sales.select_related("sold_by"), tax_label):
        lines.append(
            [
                sale.report_number,
                timezone.localtime(sale.created_at).strftime("%Y-%m-%d %H:%M"),
                sale.customer.name if sale.customer else "Walk-in Customer",
                sale.sold_by.get_username() if sale.sold_by else "",
                "Credit" if sale.is_credit else sale.get_payment_method_display(),
                f"{sale.report_subtotal:.2f}",
                f"{sale.report_discount_amount:.2f}",
                f"{sale.report_tax_amount:.2f}",
                f"{quantize_money(sale.total_amount):.2f}",
                f"{sale.report_cost_total:.2f}",
                f"{sale.report_gross_profit:.2f}",
                f"{quantize_money(sale.paid_amount):.2f}",
                f"{sale.report_balance_due:.2f}",
                f"{sale.report_returned_total:.2f}",
            ]
        )
    return csv_response(report_filename("sales-by-invoice", start, end), header, lines)


@module_access_required("reports")
def daily_sales_report(request):
    today = timezone.localdate()
    start, end = parse_report_range(request.GET, today, None)
    tax_label = get_system_settings().tax_label

    sales = (
        Sale.objects.filter(range_q(start, end))
        .select_related("customer")
        .prefetch_related("items")
        .order_by("-created_at")
    )
    if request.GET.get("export") == "csv":
        return _sales_csv(sales, tax_label, start, end)

    # Totals come from database aggregates over the whole range; only the visible
    # page of invoices is loaded to work out the per-invoice columns.
    totals = sales_totals(start, end)
    page = Paginator(sales, SALES_PAGE_SIZE).get_page(request.GET.get("page"))
    sales_rows = annotate_sale_rows(page.object_list, tax_label)

    collection_rate = quantize_money(
        (totals["paid"] / totals["total"]) * Decimal("100")
        if totals["total"]
        else Decimal("0")
    )
    gross_margin = quantize_money(
        (totals["gross_profit"] / totals["net_sales"]) * Decimal("100")
        if totals["net_sales"]
        else Decimal("0")
    )
    context = report_shell_context(request, "daily-sales", start=start, end=end)
    context.update(
        {
            "report_title": "Sales by Invoice",
            "report_description": "Billed sales, collections, cost, and gross profit invoice by invoice.",
            "report_icon": "bi-receipt",
            "sales": sales_rows,
            "page_obj": page,
            "totals": totals,
            "collection_rate": collection_rate,
            "gross_margin": gross_margin,
            "tax_label": tax_label,
        }
    )
    return render(request, "pos/daily_sales_report.html", context)


def _low_stock_report():
    rows = []
    for product in low_stock_products().select_related("supplier"):
        shortfall = quantize_qty(max(Decimal(product.low_stock_threshold or 0) - Decimal(product.stock or 0), Decimal("0")))
        rows.append(
            {
                "product": product.display_name,
                "sku": product.sku,
                "product_url": reverse("product_view", args=[product.id]),
                "category": product.category.name if product.category else "",
                "supplier": product.supplier.name if product.supplier else "",
                "stock": product.stock,
                "threshold": product.low_stock_threshold,
                "shortfall": shortfall,
                "unit": product.unit,
                "location": product.location,
            }
        )
    out_of_stock = sum(1 for row in rows if Decimal(row["stock"] or 0) <= 0)
    return Report(
        columns=[
            Column("product", "Product", url_key="product_url", note_key="sku"),
            Column("category", "Category"),
            Column("supplier", "Supplier"),
            Column("stock", "On Hand", QTY, unit_key="unit"),
            Column("threshold", "Reorder Level", QTY, unit_key="unit"),
            Column("shortfall", "Short By", QTY, unit_key="unit"),
            Column("location", "Location"),
        ],
        rows=rows,
        cards=[
            {"label": "Products to Reorder", "value": len(rows), "kind": INT, "accent": "danger", "meta": "At or below their reorder level"},
            {"label": "Out of Stock", "value": out_of_stock, "kind": INT, "accent": "warning", "meta": "Nothing left to sell"},
        ],
        empty_message="No low-stock products right now.",
    )


@module_access_required("reports")
def low_stock_list(request):
    report = _low_stock_report()
    if request.GET.get("export") == "csv":
        return report_csv_response(report, report_filename("low-stock", timezone.localdate()))

    page = Paginator(report.rows, SALES_PAGE_SIZE).get_page(request.GET.get("page"))
    context = report_shell_context(request, "low-stock", range_mode="none")
    context.update(
        {
            "report_title": "Low Stock",
            "report_description": "Products at or below their configured reorder threshold.",
            "report_icon": "bi-exclamation-triangle",
            "report_icon_tone": "warning",
            "report": report,
            "columns": report.columns,
            "table_rows": report_table_rows(report, page.object_list),
            "total_cells": [],
            "page_obj": page,
            "products": low_stock_products(),
        }
    )
    return render(request, "pos/low_stock_list.html", context)

