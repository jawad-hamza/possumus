"""Parties home, customer/supplier lists and details, statement page."""
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render

from accounts.services.cash_bank import bank_account_queryset
from accounts.services.party_badges import get_customer_badge, get_supplier_badge
from ..models import Customer, Supplier
from ..services.access import module_access_required
from ..services.field_sales import apply_customer_area_filters, customer_area_filters
from ..services.invoice_quantity import QTY_LABEL
from ..services.reporting import (
    apply_party_badges,
    filter_parties_by_balance,
    party_balance_summary,
    sales_totals,
    with_ledger_balance,
)

PARTY_PAGE_SIZE = 24


@module_access_required("parties")
def parties_home(request):
    recent_customers = Customer.objects.order_by("-created_at", "-id")[:5]
    recent_suppliers = Supplier.objects.order_by("-created_at", "-id")[:5]
    return render(
        request,
        "parties/parties_home.html",
        {
            "total_customers": Customer.objects.count(),
            "total_suppliers": Supplier.objects.count(),
            "recent_customers": recent_customers,
            "recent_suppliers": recent_suppliers,
        },
    )


def _party_list_context(request, model, party_kind):
    """Search, balance filter and pagination with the balances worked out in the same query."""
    search = request.GET.get("search", "").strip()
    balance_filter = request.GET.get("balance", "").strip()
    queryset = with_ledger_balance(model.objects.all())
    # Customers can also be narrowed by city, route and order booker.
    area_filters = customer_area_filters(request) if party_kind == "customer" else {}
    if area_filters:
        queryset = apply_customer_area_filters(queryset.select_related("route", "order_booker"), area_filters)

    if search:
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(phone__icontains=search)
            | Q(email__icontains=search)
            | Q(address__icontains=search)
            | Q(city__icontains=search)
        )
    queryset = filter_parties_by_balance(queryset, party_kind, balance_filter)
    if balance_filter in ("receivable", "payable", "advance"):
        # Biggest balance first: that is who to call or pay first.
        we_owe_them = balance_filter == "payable" or (party_kind == "customer" and balance_filter == "advance")
        queryset = queryset.order_by("ledger_balance" if we_owe_them else "-ledger_balance", "name")
    else:
        queryset = queryset.order_by("name")

    page = Paginator(queryset, PARTY_PAGE_SIZE).get_page(request.GET.get("page"))
    apply_party_badges(page.object_list, party_kind)

    page_query = request.GET.copy()
    page_query.pop("page", None)
    return {
        "page_obj": page,
        "page_querystring": page_query.urlencode(),
        "search_term": search,
        "balance_filter": balance_filter,
        "balance_summary": party_balance_summary(model, active_only=False),
        "total_count": page.paginator.count,
        "area_filters": list(area_filters.values()),
        "area_filter_active": any(entry["active"] for entry in area_filters.values()),
    }


@module_access_required("parties")
def customer_list(request):
    context = _party_list_context(request, Customer, "customer")
    context.update(
        {
            "customers": context["page_obj"].object_list,
            "total_customers": context["total_count"],
            "balance_filters": [
                ("", "All"),
                ("receivable", "Owe you"),
                ("advance", "Paid in advance"),
                ("clear", "Clear"),
            ],
        }
    )
    return render(request, "parties/customer_list.html", context)


@module_access_required("parties")
def supplier_list(request):
    context = _party_list_context(request, Supplier, "supplier")
    context.update(
        {
            "suppliers": context["page_obj"].object_list,
            "total_suppliers": context["total_count"],
            "balance_filters": [
                ("", "All"),
                ("payable", "You owe"),
                ("advance", "Paid in advance"),
                ("clear", "Clear"),
            ],
        }
    )
    return render(request, "parties/supplier_list.html", context)


@module_access_required("parties")
def customer_detail(request, id):
    customer = get_object_or_404(Customer.objects.select_related("route", "order_booker"), id=id)
    sales = customer.sales.order_by("-created_at")
    # Net of what the customer returned, tax included, like the invoice totals.
    customer_totals = sales_totals(customer=customer)
    total_sales = customer_totals["total"]
    badge = get_customer_badge(customer)

    return render(
        request,
        "parties/customer_detail.html",
        {
            "customer": customer,
            "sales": sales[:10],
            "total_sales": total_sales,
            "total_transactions": customer_totals["invoices"],
            "total_returns": customer_totals["returns_total"],
            "badge": badge,
            "bank_accounts": bank_account_queryset(),
            # Heading of the ledger's quantity column; it changes in one place with the quantity rule.
            "quantity_label": QTY_LABEL,
        },
    )


@module_access_required("parties")
def supplier_detail(request, id):
    supplier = get_object_or_404(Supplier, id=id)
    purchases = supplier.purchases.order_by("-created_at")
    total_purchases = purchases.aggregate(total=Coalesce(Sum("total_amount"), Decimal("0")))["total"]
    badge = get_supplier_badge(supplier)

    return render(
        request,
        "parties/supplier_detail.html",
        {
            "supplier": supplier,
            "purchases": purchases[:10],
            "total_purchases": total_purchases,
            "total_transactions": purchases.count(),
            "bank_accounts": bank_account_queryset(),
            "badge": badge,
        },
    )


@module_access_required("parties")
def party_statement_page(request):
    return render(request, "parties/party_statement.html")
