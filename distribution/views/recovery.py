"""Recovery: a quick entry sheet by route, and the list of what was recovered."""
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.services.bank_receipts import ACCEPT_ATTRIBUTE, receipts_required
from accounts.services.cash_bank import bank_account_queryset
from pos.models import Customer, OrderBooker, Route, quantize_money
from pos.services.reporting import parse_report_date, with_ledger_balance

from ..models import DeliveryMan, Recovery
from ..services.common import ZERO, DistributionError, routes_in_use, to_decimal
from ..services.recovery import record_recovery
from .common import can_manage, date_param, distribution_access, pk_param, tabs_context


def _customers_with_balance(route_id=None, booker_id=None, search=""):
    customers = with_ledger_balance(Customer.objects.filter(is_active=True).select_related("route", "order_booker"))
    if route_id:
        customers = customers.filter(route_id=route_id)
    if booker_id:
        customers = customers.filter(order_booker_id=booker_id)
    if search:
        customers = customers.filter(name__icontains=search)
    return list(customers.filter(ledger_balance__gt=0).order_by("route__name", "name")[:400])


@distribution_access()
def recovery_entry(request):
    source = request.POST if request.method == "POST" else request.GET
    route_id = pk_param(request, "route", source) if routes_in_use() else None
    booker_id = pk_param(request, "order_booker", source) if routes_in_use() else None
    search = (source.get("q") or "").strip()
    typed = {}
    if request.method == "POST":
        if not can_manage(request.user):
            messages.error(request, "Your role can open this area, but it cannot post changes here.")
            return redirect("distribution_recovery")
        collected_by = request.POST.get("collected_by") or Recovery.BY_OFFICE
        collector_booker = OrderBooker.objects.filter(pk=pk_param(request, "collector_booker", request.POST)).first()
        delivery_man = DeliveryMan.objects.filter(pk=pk_param(request, "delivery_man", request.POST)).first()
        bank = bank_account_queryset().filter(pk=pk_param(request, "bank_account", request.POST)).first()
        collected_on = parse_report_date(request.POST.get("collected_on"), timezone.localdate())
        entries = []
        for key, value in request.POST.items():
            if not key.startswith("amount_") or not key[7:].isdigit():
                continue
            raw = (value or "").strip()
            typed[key[7:]] = {"amount": raw, "method": request.POST.get(f"method_{key[7:]}") or Recovery.CASH}
            if raw:
                entries.append((int(key[7:]), raw, typed[key[7:]]["method"]))
        if not entries:
            messages.error(request, "Enter the amount collected beside at least one customer.")
        else:
            customers = Customer.objects.in_bulk([pk for pk, _, _ in entries])
            try:
                with transaction.atomic():
                    total = ZERO
                    for pk, raw, method in entries:
                        amount = to_decimal(raw)
                        customer = customers.get(pk)
                        if amount is None or amount <= 0:
                            raise DistributionError(f"{customer.name if customer else 'A customer'}: enter the amount as a number above zero.")
                        recovery = record_recovery(
                            customer=customer,
                            amount=amount,
                            payment_method=method,
                            bank_account=bank if method == Recovery.BANK else None,
                            collected_on=collected_on,
                            collected_by=collected_by,
                            collector_booker=collector_booker,
                            delivery_man=delivery_man,
                            collector_name=request.POST.get("collector_name", ""),
                            receipt_file=request.FILES.get(f"receipt_{pk}"),
                            user=request.user,
                        )
                        total += recovery.amount
            except (DistributionError, ValueError) as exc:
                messages.error(request, f"Nothing was saved. {exc}")
            else:
                messages.success(request, f"{len(entries)} recover{'ies' if len(entries) != 1 else 'y'} recorded, {total:,.2f} in all.")
                query = []
                if route_id:
                    query.append(f"route={route_id}")
                if booker_id:
                    query.append(f"order_booker={booker_id}")
                url = reverse("distribution_recovery")
                return redirect(f"{url}?{'&'.join(query)}" if query else url)

    customers = _customers_with_balance(route_id, booker_id, search)
    for customer in customers:
        entry = typed.get(str(customer.pk), {})
        customer.typed_amount = entry.get("amount", "")
        customer.typed_method = entry.get("method", Recovery.CASH)
    return render(
        request,
        "distribution/recovery_entry.html",
        {
            "customers": customers,
            "owed_total": quantize_money(sum((Decimal(c.ledger_balance) for c in customers), ZERO)),
            "route_id": route_id,
            "booker_id": booker_id,
            "search": search,
            "routes": Route.objects.filter(is_active=True) if routes_in_use() else [],
            "bookers": OrderBooker.objects.filter(is_active=True) if routes_in_use() else [],
            "delivery_men": DeliveryMan.objects.filter(is_active=True),
            "bank_accounts": list(bank_account_queryset()),
            "collector_choices": Recovery.COLLECTOR_CHOICES,
            "posted": request.POST if request.method == "POST" else {},
            "today": timezone.localdate(),
            "receipt_accept": ACCEPT_ATTRIBUTE,
            "receipt_required": receipts_required(),
            "can_manage": can_manage(request.user),
            **tabs_context("recovery"),
        },
    )


@distribution_access()
def recovery_list(request):
    today = timezone.localdate()
    start = date_param(request, "start", today.replace(day=1))
    end = date_param(request, "end", today)
    if end < start:
        start, end = end, start
    rows = Recovery.objects.filter(collected_on__gte=start, collected_on__lte=end).select_related(
        "customer", "route", "load_form", "sale", "settlement"
    )
    route_id = pk_param(request, "route")
    if route_id:
        rows = rows.filter(route_id=route_id)
    collector = (request.GET.get("collector") or "").strip()
    if collector:
        rows = rows.filter(collector_name=collector)
    totals = rows.aggregate(total=Sum("amount"))
    page = Paginator(rows.order_by("-collected_on", "-id"), 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "distribution/recovery_list.html",
        {
            "recoveries": list(page.object_list),
            "page_obj": page,
            "page_querystring": query.urlencode(),
            "start": start,
            "end": end,
            "route_id": route_id,
            "collector": collector,
            "collectors": Recovery.objects.exclude(collector_name="").values_list("collector_name", flat=True).distinct().order_by("collector_name"),
            "routes": Route.objects.filter(is_active=True) if routes_in_use() else [],
            "total": quantize_money(totals["total"] or ZERO),
            **tabs_context("recovery"),
        },
    )
