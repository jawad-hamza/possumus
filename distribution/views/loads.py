"""Load forms: the list, a load with its invoices, the printed load sheet and the day-end settlement."""
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.services.bank_receipts import ACCEPT_ATTRIBUTE, receipts_required
from accounts.services.cash_bank import bank_account_queryset
from pos.models import Customer, OrderBooker, Route, SaleItem, quantize_money
from pos.services.access import ACCESS_MANAGE
from pos.services.business_rules import get_active_company_profile
from pos.services.navigation import return_context, with_touched_row
from pos.services.returns import return_row

from ..forms import CloseLoadForm, LoadFormForm
from ..models import DeliveryMan, LoadForm, Recovery
from ..services.common import ZERO, DistributionError, routes_in_use, to_decimal
from ..services.loads import (
    add_invoices,
    close_load,
    create_load,
    dispatch_load,
    load_sheet,
    loadable_sales,
    post_load_returns,
    remove_invoice,
    reopen_load,
    settlement_figures,
    update_load,
)
from ..services.recovery import record_recovery
from .common import can_manage, date_param, distribution_access, pk_param, quick_panels, tabs_context

STATUS_FILTERS = (("", "All"), (LoadForm.OPEN, "Being loaded"), (LoadForm.DISPATCHED, "Out for delivery"), (LoadForm.SETTLED, "Settled"))


@distribution_access()
def load_list(request):
    status = request.GET.get("status") or ""
    if status not in dict(STATUS_FILTERS):
        status = ""
    start = date_param(request, "start")
    end = date_param(request, "end")
    loads = LoadForm.objects.select_related("route", "delivery_man", "vehicle").annotate(invoice_count=Count("invoices"))
    if status:
        loads = loads.filter(status=status)
    if start:
        loads = loads.filter(load_date__gte=start)
    if end:
        loads = loads.filter(load_date__lte=end)
    if pk_param(request, "route"):
        loads = loads.filter(route_id=pk_param(request, "route"))
    if pk_param(request, "delivery_man"):
        loads = loads.filter(delivery_man_id=pk_param(request, "delivery_man"))
    page = Paginator(loads.order_by("-load_date", "-id"), 50).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    values = dict(
        LoadForm.objects.filter(pk__in=[row.pk for row in rows])
        .annotate(value=Sum("invoices__sale__total_amount"))
        .values_list("pk", "value")
    )
    for row in rows:
        row.bill_value = quantize_money(values.get(row.pk) or ZERO)
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "distribution/load_list.html",
        {
            "loads": rows,
            "page_obj": page,
            "page_querystring": query.urlencode(),
            "status": status,
            "status_filters": STATUS_FILTERS,
            "start": start,
            "end": end,
            "route_id": pk_param(request, "route"),
            "delivery_man_id": pk_param(request, "delivery_man"),
            "routes": Route.objects.filter(is_active=True) if routes_in_use() else [],
            "delivery_men": DeliveryMan.objects.filter(is_active=True),
            "can_manage": can_manage(request.user),
            **tabs_context("loads"),
        },
    )


def _load_form(request, load=None):
    back = return_context(request, reverse("distribution_load_list"))
    form = LoadFormForm(request.POST or None, instance=load or LoadForm())
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                if load is None:
                    saved = create_load(
                        load_date=data["load_date"],
                        route=data.get("route"),
                        delivery_man=data.get("delivery_man"),
                        vehicle=data.get("vehicle"),
                        notes=data.get("notes", ""),
                        user=request.user,
                    )
                else:
                    saved = update_load(
                        load,
                        load_date=data["load_date"],
                        route=data.get("route") if "route" in form.fields else load.route,
                        delivery_man=data.get("delivery_man"),
                        vehicle=data.get("vehicle"),
                        notes=data.get("notes", ""),
                        user=request.user,
                    )
            except DistributionError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Load {saved.display_number} {'saved' if load else 'opened'}.")
                return redirect("distribution_load_detail", pk=saved.pk)
        else:
            messages.error(request, "Please fix the load details below.")
    return render(
        request,
        "distribution/load_form.html",
        {"form": form, "load": load, **back, **quick_panels(request, "delivery_man", "vehicle"), **tabs_context("loads")},
    )


@distribution_access(ACCESS_MANAGE)
def load_add(request):
    return _load_form(request)


@distribution_access(ACCESS_MANAGE)
def load_edit(request, pk):
    load = get_object_or_404(LoadForm, pk=pk)
    if load.is_settled:
        messages.info(request, f"{load.display_number} is settled and can no longer be changed.")
        return redirect("distribution_load_detail", pk=load.pk)
    return _load_form(request, load)


@distribution_access()
def load_detail(request, pk):
    load = get_object_or_404(LoadForm.objects.select_related("route", "delivery_man", "vehicle"), pk=pk)
    sheet = load_sheet(load)
    candidates = []
    pick = {}
    if load.is_open:
        pick_date = date_param(request, "date", load.load_date)
        pick_route_id = pk_param(request, "route") if "route" in request.GET else load.route_id
        pick_booker_id = pk_param(request, "order_booker")
        pick_orders = request.GET.get("from_orders") == "1"
        candidates = list(
            loadable_sales(
                load_date=pick_date if request.GET.get("any_date") != "1" else None,
                route=Route.objects.filter(pk=pick_route_id).first() if pick_route_id and routes_in_use() else None,
                order_booker=OrderBooker.objects.filter(pk=pick_booker_id).first() if pick_booker_id and routes_in_use() else None,
                from_orders=pick_orders,
            )[:300]
        )
        pick = {
            "date": pick_date,
            "route_id": pick_route_id,
            "booker_id": pick_booker_id,
            "from_orders": pick_orders,
            "any_date": request.GET.get("any_date") == "1",
        }
    return render(
        request,
        "distribution/load_detail.html",
        {
            "load": load,
            "sheet": sheet,
            "candidates": candidates,
            "candidate_total": quantize_money(sum((sale.total_amount for sale in candidates), ZERO)),
            "pick": pick,
            "routes": Route.objects.filter(is_active=True) if routes_in_use() else [],
            "bookers": OrderBooker.objects.filter(is_active=True) if routes_in_use() else [],
            "can_manage": can_manage(request.user),
            **tabs_context("loads"),
        },
    )


@distribution_access(ACCESS_MANAGE)
@require_POST
def load_add_invoices(request, pk):
    load = get_object_or_404(LoadForm, pk=pk)
    try:
        count = add_invoices(load, request.POST.getlist("sale"), user=request.user)
    except DistributionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{count} invoice{'s' if count != 1 else ''} put on {load.display_number}.")
    return redirect("distribution_load_detail", pk=load.pk)


@distribution_access(ACCESS_MANAGE)
@require_POST
def load_remove_invoice(request, pk, sale_id):
    load = get_object_or_404(LoadForm, pk=pk)
    try:
        number = remove_invoice(load, sale_id, user=request.user)
    except DistributionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{number} taken off {load.display_number}.")
    return redirect("distribution_load_detail", pk=load.pk)


@distribution_access(ACCESS_MANAGE)
@require_POST
def load_dispatch(request, pk):
    load = get_object_or_404(LoadForm, pk=pk)
    try:
        dispatch_load(load, user=request.user)
    except DistributionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{load.display_number} is out for delivery. Settle it when the van is back.")
    return redirect("distribution_load_detail", pk=load.pk)


@distribution_access(ACCESS_MANAGE)
@require_POST
def load_reopen(request, pk):
    load = get_object_or_404(LoadForm, pk=pk)
    try:
        reopen_load(load, user=request.user)
    except DistributionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{load.display_number} is back to loading.")
    return redirect("distribution_load_detail", pk=load.pk)


@distribution_access()
def load_print(request, pk):
    load = get_object_or_404(LoadForm.objects.select_related("route", "delivery_man", "vehicle"), pk=pk)
    return render(
        request,
        "distribution/load_print.html",
        {"load": load, "sheet": load_sheet(load), "company": get_active_company_profile()},
    )


# ------------------------------------------------------------------------- settlement
def _return_entries(request, load):
    """{sale id: {"quantities": {item id: qty}, "units": {item id: level}}} from the settlement form."""
    entries = {}
    items = SaleItem.objects.filter(sale__load_entry__load_form=load).values_list("pk", "sale_id")
    for item_id, sale_id in items:
        raw = (request.POST.get(f"ret_{item_id}") or "").strip()
        if not raw:
            continue
        quantity = to_decimal(raw)
        if quantity is None or quantity < 0:
            raise DistributionError("Enter returned quantities as numbers.")
        if quantity == 0:
            continue
        entry = entries.setdefault(sale_id, {"quantities": {}, "units": {}})
        entry["quantities"][item_id] = quantity
        unit = request.POST.get(f"unit_{item_id}")
        if unit:
            entry["units"][item_id] = unit
    return entries


def _settle_post(request, load):
    action = request.POST.get("action")
    if action == "returns":
        entries = _return_entries(request, load)
        created = post_load_returns(load, entries, reason=request.POST.get("reason"), user=request.user)
        total = quantize_money(sum((row.total_amount for row in created), ZERO))
        messages.success(request, f"{len(created)} return{'s' if len(created) != 1 else ''} posted, {total:,.2f} in all. Stock and the customers' accounts are updated.")
    elif action == "recovery":
        customer = Customer.objects.filter(pk=pk_param(request, "customer", request.POST)).first()
        method = request.POST.get("payment_method") or Recovery.CASH
        bank = bank_account_queryset().filter(pk=pk_param(request, "bank_account", request.POST)).first() if method == Recovery.BANK else None
        sale_id = pk_param(request, "sale", request.POST)
        sale = load.invoices.filter(sale_id=sale_id).first().sale if sale_id and load.invoices.filter(sale_id=sale_id).exists() else None
        if not load.is_dispatched:
            raise DistributionError("Recoveries are recorded while the load is out for delivery.")
        recovery = record_recovery(
            customer=customer,
            amount=to_decimal(request.POST.get("amount"), ZERO),
            payment_method=method,
            bank_account=bank,
            collected_by=Recovery.BY_DELIVERY_MAN if load.delivery_man_id else Recovery.BY_OFFICE,
            delivery_man=load.delivery_man,
            load_form=load,
            sale=sale,
            receipt_file=request.FILES.get("bank_receipt"),
            user=request.user,
        )
        messages.success(request, f"Recovery of {recovery.amount:,.2f} from {recovery.customer.name} recorded.")
    elif action == "close":
        form = CloseLoadForm(request.POST)
        if not form.is_valid():
            raise DistributionError("Enter the cash the delivery man handed in (0 when he brought none).")
        closed = close_load(load, cash_received=form.cleaned_data["cash_received"], notes=form.cleaned_data.get("notes", ""), user=request.user)
        if closed.cash_variance < 0:
            messages.warning(request, f"{closed.display_number} settled. Cash short by {-closed.cash_variance:,.2f}; posted to Cash Short and Over.")
        elif closed.cash_variance > 0:
            messages.warning(request, f"{closed.display_number} settled. Cash over by {closed.cash_variance:,.2f}; posted to Cash Short and Over.")
        else:
            messages.success(request, f"{closed.display_number} settled. The cash matches.")
    else:
        raise DistributionError("Unknown action.")


@distribution_access()
def load_settle(request, pk):
    load = get_object_or_404(LoadForm.objects.select_related("route", "delivery_man", "vehicle"), pk=pk)
    if request.method == "POST":
        try:
            _settle_post(request, load)
        except (DistributionError, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect(with_touched_row(reverse("distribution_load_settle", args=[load.pk]), None))

    if load.is_open:
        messages.info(request, f"{load.display_number} has not left yet. Settle it when it comes back.")
        return redirect("distribution_load_detail", pk=load.pk)
    figures = settlement_figures(load)
    invoice_rows = []
    sale_ids = [row["sale"].pk for row in figures["invoices"]]
    items = (
        SaleItem.objects.filter(sale_id__in=sale_ids)
        .select_related("product__category", "sale")
        .prefetch_related("return_items")
        .order_by("sale_id", "id")
    )
    by_sale = {}
    for item in items:
        by_sale.setdefault(item.sale_id, []).append(return_row(item))
    for row in figures["invoices"]:
        invoice_rows.append({**row, "lines": by_sale.get(row["sale"].pk, [])})
    customers = sorted({row["sale"].customer for row in figures["invoices"] if row["sale"].customer}, key=lambda c: c.name)
    return render(
        request,
        "distribution/load_settle.html",
        {
            "load": load,
            "figures": figures,
            "invoice_rows": invoice_rows,
            "customers": customers,
            "bank_accounts": list(bank_account_queryset()),
            "close_form": CloseLoadForm(initial={"cash_received": figures["cash_expected"]}),
            "receipt_accept": ACCEPT_ATTRIBUTE,
            "receipt_required": receipts_required(),
            "can_manage": can_manage(request.user),
            **tabs_context("loads"),
        },
    )
