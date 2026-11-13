"""Sales orders: the list with its filters and bulk invoicing, the order form, one order."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from pos.models import Customer, OrderBooker, Product, Route, SystemSettings, format_qty, quantize_money
from pos.services.access import ACCESS_MANAGE
from pos.services.navigation import return_context, safe_next_url, with_touched_row
from pos.services.reporting import range_label

from ..forms import CancelOrderForm, OrderHeaderForm
from ..models import SalesOrder, SalesOrderItem
from ..services.common import ZERO, DistributionError, parse_pack_lines, product_choice_data, routes_in_use
from ..services.orders import cancel_order, conversion_messages, customer_terms, invoice_order, invoice_orders, save_order
from .common import can_manage, date_param, distribution_access, pk_param, quick_panels, tabs_context

STATUS_FILTERS = (
    ("booked", "Booked"),
    ("invoiced", "Invoiced"),
    ("cancelled", "Cancelled"),
    ("all", "All"),
)


def _flash(request, lines):
    for level, text in lines:
        getattr(messages, level)(request, text)


def _credit_choice(request):
    return (request.POST.get("payment") or "credit") != "cash"


# ----------------------------------------------------------------------------- the list
@distribution_access()
def order_list(request):
    status = request.GET.get("status") or "booked"
    if status not in dict(STATUS_FILTERS):
        status = "booked"
    start = date_param(request, "start")
    end = date_param(request, "end")
    if start and end and end < start:
        start, end = end, start
    route_id = pk_param(request, "route")
    booker_id = pk_param(request, "order_booker")

    orders = SalesOrder.objects.select_related("customer", "route", "order_booker", "sale").prefetch_related(
        Prefetch("items", queryset=SalesOrderItem.objects.select_related("product"))
    )
    if status != "all":
        orders = orders.filter(status=status)
    if start:
        orders = orders.filter(order_date__gte=start)
    if end:
        orders = orders.filter(order_date__lte=end)
    if route_id:
        orders = orders.filter(route_id=route_id)
    if booker_id:
        orders = orders.filter(order_booker_id=booker_id)
    orders = orders.order_by("-order_date", "-id")

    page = Paginator(orders, 50).get_page(request.GET.get("page"))
    rows = list(page.object_list)
    status_counts = dict(SalesOrder.objects.values_list("status").annotate(count=Count("id")).order_by())
    query = request.GET.copy()
    query.pop("page", None)
    routes = list(Route.objects.filter(is_active=True)) if routes_in_use() else []
    return render(
        request,
        "distribution/order_list.html",
        {
            "orders": rows,
            "page_obj": page,
            "page_querystring": query.urlencode(),
            "status": status,
            "status_filters": [(value, label, status_counts.get(value)) for value, label in STATUS_FILTERS],
            "start": start,
            "end": end,
            "range_label": range_label(start, end) if (start or end) else "",
            "route_id": route_id,
            "booker_id": booker_id,
            "routes": routes,
            "bookers": OrderBooker.objects.filter(is_active=True) if routes_in_use() else [],
            "page_total": quantize_money(sum((order.total_amount for order in rows), ZERO)),
            "total_span": 2 + (1 if routes else 0) + (1 if can_manage(request.user) and status == "booked" else 0),
            "can_manage": can_manage(request.user),
            "allow_credit": SystemSettings.load().allow_credit_sales,
            **tabs_context("orders"),
        },
    )


@distribution_access(ACCESS_MANAGE)
@require_POST
def order_bulk_invoice(request):
    ids = [int(value) for value in request.POST.getlist("order") if str(value).isdigit()]
    back = safe_next_url(request) or reverse("distribution_order_list")
    if not ids:
        messages.error(request, "Tick the booked orders to invoice.")
        return redirect(back)
    orders = list(SalesOrder.objects.filter(pk__in=ids).select_related("customer").order_by("order_date", "id"))
    results = invoice_orders(orders, user=request.user, credit=_credit_choice(request))
    _flash(request, conversion_messages(results))
    return redirect(back)


# ------------------------------------------------------------------------------ one order
def _product_catalogue():
    settings_row = SystemSettings.load()
    products = list(Product.objects.filter(is_active=True).select_related("category").order_by("name", "variant_label"))
    data = {str(product.pk): product_choice_data(product, settings_row.till_default_unit) for product in products}
    return products, data


def _typed_rows_from_order(order):
    return [
        {
            "product_id": str(item.product_id),
            "quantity": format_qty(item.quantity),
            "unit": item.unit_level,
            "rate": f"{item.price:.2f}",
            "discount": format_qty(item.discount_percent),
            "error": "",
        }
        for item in order.items.all()
    ]


def _order_form(request, order=None):
    back = return_context(request, reverse("distribution_order_list"))
    form = OrderHeaderForm(request.POST or None, order=order, initial=_initial_for(order, request))
    products, product_data = _product_catalogue()
    typed = _typed_rows_from_order(order) if order is not None else []
    if request.method == "POST":
        lines, problems, typed = parse_pack_lines(request.POST, with_discount=True)
        if form.is_valid() and not problems:
            data = form.cleaned_data
            customer = data["customer"]
            route = data.get("route")
            booker = data.get("order_booker")
            if routes_in_use() and order is None and route is None and booker is None:
                # Nothing chosen: the order takes the customer's own route and order booker.
                terms = customer_terms(customer)
                route = Route.objects.filter(pk=terms["route_id"]).first() if terms["route_id"] else None
                booker = OrderBooker.objects.filter(pk=terms["order_booker_id"]).first() if terms["order_booker_id"] else None
            try:
                saved = save_order(
                    order=order,
                    customer=customer,
                    lines=lines,
                    user=request.user,
                    route=route,
                    order_booker=booker,
                    order_date=data["order_date"],
                    delivery_date=data.get("delivery_date"),
                    notes=data.get("notes", ""),
                )
            except DistributionError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f"Order {saved.display_number} for {saved.customer.name} {'saved' if order else 'booked'}, {saved.total_amount:,.2f}.",
                )
                if request.POST.get("then") == "new":
                    url = reverse("distribution_order_add")
                    if back["next_url"]:
                        url += f"?next={back['next_url']}"
                    return redirect(url)
                return redirect(with_touched_row(back["back_url"], saved.pk))
        else:
            for problem in problems:
                messages.error(request, problem)
            if not form.is_valid():
                messages.error(request, "Please fix the order details below.")
    if not typed:
        typed = [{"product_id": "", "quantity": "", "unit": "", "rate": "", "discount": "0", "error": ""}]
    customer_id = (request.POST.get("customer") if request.method == "POST" else None) or (order.customer_id if order else form.initial.get("customer"))
    customer = Customer.objects.filter(pk=customer_id).select_related("route", "order_booker").first() if str(customer_id or "").isdigit() else None
    settings_row = SystemSettings.load()
    return render(
        request,
        "distribution/order_form.html",
        {
            "form": form,
            "order": order,
            "lines": typed,
            "products": products,
            "product_data": product_data,
            "customer_terms": customer_terms(customer) if customer else None,
            "allow_price_override": settings_row.allow_price_override,
            "allow_line_discount": settings_row.allow_line_discount,
            "customer_add_url": f"{reverse('customer_add')}?next={request.get_full_path()}",
            **quick_panels(request, "customer"),
            **back,
            **tabs_context("orders"),
        },
    )


def _initial_for(order, request):
    if order is not None:
        return {
            "customer": order.customer_id,
            "route": order.route_id,
            "order_booker": order.order_booker_id,
            "order_date": order.order_date,
            "delivery_date": order.delivery_date,
            "notes": order.notes,
        }
    initial = {}
    customer_id = pk_param(request, "customer")
    if customer_id:
        initial["customer"] = customer_id
    return initial


@distribution_access(ACCESS_MANAGE)
def order_add(request):
    return _order_form(request)


@distribution_access(ACCESS_MANAGE)
def order_edit(request, pk):
    order = get_object_or_404(SalesOrder.objects.prefetch_related("items"), pk=pk)
    if order.status != SalesOrder.BOOKED:
        messages.info(request, f"{order.display_number} is {order.get_status_display().lower()} and can no longer be changed.")
        return redirect("distribution_order_detail", pk=order.pk)
    return _order_form(request, order)


@distribution_access()
def order_detail(request, pk):
    order = get_object_or_404(
        SalesOrder.objects.select_related("customer", "route", "order_booker", "sale", "created_by").prefetch_related(
            Prefetch("items", queryset=SalesOrderItem.objects.select_related("product"))
        ),
        pk=pk,
    )
    return render(
        request,
        "distribution/order_detail.html",
        {
            "order": order,
            "items": list(order.items.all()),
            "terms": customer_terms(order.customer),
            "cancel_form": CancelOrderForm(),
            "can_manage": can_manage(request.user),
            "allow_credit": SystemSettings.load().allow_credit_sales,
            **return_context(request, reverse("distribution_order_list")),
            **tabs_context("orders"),
        },
    )


@distribution_access(ACCESS_MANAGE)
@require_POST
def order_invoice(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    result = invoice_order(order, user=request.user, credit=_credit_choice(request))
    _flash(request, conversion_messages([result]))
    return redirect("distribution_order_detail", pk=order.pk)


@distribution_access(ACCESS_MANAGE)
@require_POST
def order_cancel(request, pk):
    order = get_object_or_404(SalesOrder, pk=pk)
    form = CancelOrderForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Say why the order is cancelled.")
        return redirect("distribution_order_detail", pk=order.pk)
    try:
        cancel_order(order, reason=form.cleaned_data["reason"], user=request.user)
    except DistributionError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Order {order.display_number} cancelled.")
    return redirect("distribution_order_detail", pk=order.pk)


@distribution_access()
def customer_terms_json(request, pk):
    customer = get_object_or_404(Customer.objects.select_related("route", "order_booker"), pk=pk)
    return JsonResponse({"status": "ok", "customer": customer_terms(customer)})
