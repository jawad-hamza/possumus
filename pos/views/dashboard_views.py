"""Dashboard."""
from decimal import Decimal

from django.shortcuts import render
from django.urls import reverse

from ..models import Customer, Purchase, Sale, Supplier, quantize_money
from ..services.access import module_access_required
from ..services.currency import format_money
from ..services.modules import module_enabled
from ..services.reporting import (
    best_selling_products,
    business_snapshot,
    party_balance_summary,
    sales_trend,
    top_customers,
)


@module_access_required("dashboard")
def dashboard(request):
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
    month_purchase_total = snapshot["month_purchase_total"]
    expense_due_total = snapshot["expense_due_total"]
    expense_alert_count = snapshot["expense_alert_count"]
    today_report_url = snapshot["today_report_url"]
    week_report_url = snapshot["week_report_url"]
    expiry_report_url = snapshot["expiry_report_url"]

    recent_sales = Sale.objects.select_related("customer").order_by("-created_at")[:8]
    recent_purchases = Purchase.objects.select_related("supplier").order_by("-created_at")[:8]

    # One grouped query per party type instead of one balance query per party.
    customer_balances = party_balance_summary(Customer)
    supplier_balances = party_balance_summary(Supplier)
    customer_receivable_total = customer_balances["owed_to_us"]
    customer_receivable_count = customer_balances["owed_to_us_count"]
    supplier_payable_total = supplier_balances["owed_by_us"]
    supplier_payable_count = supplier_balances["owed_by_us_count"]

    month_margin_percent = quantize_money(
        (month_sales["gross_profit"] / month_sales["net_sales"]) * Decimal("100")
        if month_sales["net_sales"]
        else Decimal("0")
    )
    today_collection_rate = quantize_money(
        (today_sales["paid"] / today_sales["total"]) * Decimal("100")
        if today_sales["total"]
        else Decimal("0")
    )
    dashboard_kpis = [
        {
            "title": "Today Net Sales",
            "value": today_sales["net_sales"],
            "meta": f"{today_sales['invoices']} invoices today",
            "url": today_report_url,
            "kind": "money",
            "accent": "primary",
            "icon": "bi-cash-stack",
        },
        {
            "title": "Today Gross Profit",
            "value": today_sales["gross_profit"],
            "meta": f"Collection rate {today_collection_rate:.2f}%",
            "url": today_report_url,
            "kind": "money",
            "accent": "success",
            "icon": "bi-graph-up-arrow",
        },
        {
            "title": "Last 7 Days Sales",
            "value": week_sales["net_sales"],
            "meta": f"Avg invoice {format_money(week_sales['avg_invoice'])}",
            "url": week_report_url,
            "kind": "money",
            "accent": "info",
            "icon": "bi-calendar-week",
        },
        {
            "title": "This Month Purchases",
            "module": "purchases",
            "value": month_purchase_total,
            "meta": "Inventory bought this month",
            "url": reverse("purchase_list"),
            "kind": "money",
            "accent": "warning",
            "icon": "bi-box-seam",
        },
        {
            "title": "Low Stock Alerts",
            "module": "reports",
            "value": low_stock_count,
            "meta": "Products below reorder point",
            "url": reverse("low_stock_list"),
            "kind": "number",
            "accent": "danger",
            "icon": "bi-exclamation-triangle",
        },
        {
            "title": "Expense Bills Due",
            "module": "expenses",
            "value": expense_due_total,
            "meta": "Unpaid operating bills",
            "url": reverse("expense_list"),
            "kind": "money",
            "accent": "danger",
            "icon": "bi-receipt-cutoff",
        },
        {
            "title": "Customers Owe You",
            "module": "parties",
            "value": customer_receivable_total,
            "meta": f"{customer_receivable_count} {'customer owes' if customer_receivable_count == 1 else 'customers owe'} you",
            "url": reverse("customer_list"),
            "kind": "money",
            "accent": "danger",
            "icon": "bi-people",
        },
        {
            "title": "You Owe Suppliers",
            "module": "parties",
            "value": supplier_payable_total,
            "meta": f"You owe {supplier_payable_count} {'supplier' if supplier_payable_count == 1 else 'suppliers'}",
            "url": reverse("supplier_list"),
            "kind": "money",
            "accent": "slate",
            "icon": "bi-truck",
        },
    ]

    if settings_obj.enable_product_expiry:
        dashboard_kpis.insert(
            5,
            {
                "title": "Expiry Alerts",
                "value": near_expiry_count,
                "meta": f"{expired_count} already expired",
                "url": expiry_report_url,
                "kind": "number",
                "accent": "warning",
                "icon": "bi-hourglass-split",
            },
        )

    # A card whose module is switched off disappears; a sales card keeps its figure but loses the report link.
    dashboard_kpis = [kpi for kpi in dashboard_kpis if "module" not in kpi or module_enabled(kpi["module"])]
    if not module_enabled("reports"):
        for kpi in dashboard_kpis:
            if kpi["url"] in {today_report_url, week_report_url, expiry_report_url}:
                kpi["url"] = ""

    return render(
        request,
        "pos/dashboard.html",
        {
            "today": today,
            "week_start": week_start,
            "month_start": month_start,
            "today_sales": today_sales,
            "week_sales": week_sales,
            "month_sales": month_sales,
            "month_purchase_total": month_purchase_total,
            "expense_due_total": expense_due_total,
            "expense_alert_count": expense_alert_count,
            "today_collection_rate": today_collection_rate,
            "month_margin_percent": month_margin_percent,
            "dashboard_kpis": dashboard_kpis,
            "reports_center_url": reverse("reports_home"),
            "expiry_report_url": expiry_report_url,
            "near_expiry_count": near_expiry_count,
            "expired_count": expired_count,
            "low_stock_count": low_stock_count,
            "top_low_stock": snapshot["low_stock_products"][:10],
            "best_selling": best_selling_products(limit=10),
            "best_selling_month": snapshot["best_selling_month"],
            "top_customers": top_customers(limit=10),
            "recent_sales": recent_sales,
            "recent_purchases": recent_purchases,
            "sales_trend": sales_trend(today, days=7),
        },
    )
