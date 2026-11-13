"""Routes and order bookers (the "field_sales" module) and the customer and supplier edit pages.

Routes and order bookers are short master lists under the customers area: a list, an add
page and an edit page each, plus the JSON endpoints behind the "Add route" / "Add order
booker" side panels of the customer form.
"""
from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.services.audit import record_audit
from ..forms import CustomerForm, SupplierForm
from ..forms_field_sales import OrderBookerForm, QuickOrderBookerForm, QuickRouteForm, RouteForm
from ..models import Customer, OrderBooker, Route, Supplier, quantize_money
from ..services.access import ACCESS_MANAGE, any_module_access_required, feature_required, module_access_required
from ..services.field_sales import MODULE_KEY
from ..services.navigation import return_context, with_touched_row
from ..services.quick_add import quick_add_context


def _json_form_errors(form):
    return {field: [entry["message"] for entry in entries] for field, entries in form.errors.get_json_data().items()}


# ------------------------------------------------------------------------------ routes

@module_access_required("parties")
@feature_required(MODULE_KEY)
def route_list(request):
    routes = list(
        Route.objects.annotate(
            customer_count=Count("customers", distinct=True),
            active_customer_count=Count("customers", filter=Q(customers__is_active=True), distinct=True),
        ).prefetch_related("order_bookers")
    )
    for route in routes:
        route.booker_names = ", ".join(booker.name for booker in route.order_bookers.all() if booker.is_active)
    return render(
        request,
        "parties/route_list.html",
        {
            "routes": routes,
            "active_count": sum(1 for route in routes if route.is_active),
        },
    )


def _route_form(request, route=None):
    form = RouteForm(request.POST or None, instance=route)
    back = return_context(request, reverse("route_list"))
    if request.method == "POST":
        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Route '{saved.name}' {'updated' if route else 'added'}.")
            return redirect(with_touched_row(back["back_url"], saved.pk))
        messages.error(request, "Please fix the route details below.")
    return render(request, "parties/route_form.html", {"form": form, "route": route, **back})


@module_access_required("parties", minimum=ACCESS_MANAGE)
@feature_required(MODULE_KEY)
def route_add(request):
    return _route_form(request)


@module_access_required("parties", minimum=ACCESS_MANAGE)
@feature_required(MODULE_KEY)
def route_edit(request, pk):
    return _route_form(request, get_object_or_404(Route, pk=pk))


@module_access_required("parties", minimum=ACCESS_MANAGE)
@feature_required(MODULE_KEY)
@require_POST
def quick_create_route(request):
    form = QuickRouteForm(request.POST, prefix="quick_route")
    if form.is_valid():
        route = form.save()
        return JsonResponse({"status": "ok", "route": {"id": route.id, "name": route.label, "city": route.city}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


# ------------------------------------------------------------------------ order bookers

@module_access_required("parties")
@feature_required(MODULE_KEY)
def order_booker_list(request):
    bookers = list(
        OrderBooker.objects.annotate(
            customer_count=Count("customers", distinct=True),
        ).prefetch_related("routes")
    )
    for booker in bookers:
        booker.route_names = ", ".join(route.label for route in booker.routes.all())
    return render(
        request,
        "parties/order_booker_list.html",
        {
            "bookers": bookers,
            "active_count": sum(1 for booker in bookers if booker.is_active),
        },
    )


def _order_booker_form(request, booker=None):
    form = OrderBookerForm(request.POST or None, instance=booker)
    back = return_context(request, reverse("order_booker_list"))
    if request.method == "POST":
        if form.is_valid():
            saved = form.save()
            messages.success(request, f"Order booker '{saved.name}' {'updated' if booker else 'added'}.")
            return redirect(with_touched_row(back["back_url"], saved.pk))
        messages.error(request, "Please fix the order booker details below.")
    return render(
        request,
        "parties/order_booker_form.html",
        {"form": form, "booker": booker, **back, **quick_add_context(request, "route")},
    )


@module_access_required("parties", minimum=ACCESS_MANAGE)
@feature_required(MODULE_KEY)
def order_booker_add(request):
    return _order_booker_form(request)


@module_access_required("parties", minimum=ACCESS_MANAGE)
@feature_required(MODULE_KEY)
def order_booker_edit(request, pk):
    return _order_booker_form(request, get_object_or_404(OrderBooker, pk=pk))


@module_access_required("parties", minimum=ACCESS_MANAGE)
@feature_required(MODULE_KEY)
@require_POST
def quick_create_order_booker(request):
    form = QuickOrderBookerForm(request.POST, prefix="quick_order_booker")
    if form.is_valid():
        booker = form.save()
        return JsonResponse({"status": "ok", "order_booker": {"id": booker.id, "name": booker.name}})
    return JsonResponse({"status": "error", "errors": _json_form_errors(form)}, status=400)


# ------------------------------------------------------------------------ customer edit

@module_access_required("parties", minimum=ACCESS_MANAGE)
def customer_edit(request, id):
    customer = get_object_or_404(Customer, id=id)
    limit_before = customer.credit_limit
    form = CustomerForm(request.POST or None, instance=customer)
    back = return_context(request, reverse("customer_detail", args=[customer.id]))
    if request.method == "POST":
        if form.is_valid():
            customer = form.save()
            if customer.credit_limit != limit_before:
                record_audit(
                    action="customer_credit_limit_changed",
                    description=f"Credit limit of customer '{customer.name}' changed.",
                    actor=request.user,
                    target=customer,
                    metadata={
                        "before": "" if limit_before is None else str(quantize_money(limit_before)),
                        "after": "" if customer.credit_limit is None else str(quantize_money(customer.credit_limit)),
                    },
                )
            messages.success(request, f"Customer '{customer.name}' updated.")
            return redirect(_back_after_edit(back, reverse("customer_detail", args=[customer.id]), customer.id))
        messages.error(request, "Please fix the customer details below.")

    return render(
        request,
        "pos/add_customer.html",
        {
            "form": form,
            "customer": customer,
            "opening_form": None,
            "next_url": back["back_url"],
            "return_to_pos": False,
        },
    )


def _back_after_edit(back, own_page, pk):
    """Where an edit page goes after saving: the party's own page, or the list it was opened from with the row marked."""
    if back["back_url"] == own_page:
        return own_page
    return with_touched_row(back["back_url"], pk)


# ------------------------------------------------------------------------ supplier edit

@any_module_access_required(("purchases", "parties"), minimum=ACCESS_MANAGE)
def supplier_edit(request, id):
    """Suppliers gained a city; this is where an existing supplier gets one."""
    supplier = get_object_or_404(Supplier, id=id)
    form = SupplierForm(request.POST or None, instance=supplier)
    back = return_context(request, reverse("supplier_detail", args=[supplier.id]))
    if request.method == "POST":
        if form.is_valid():
            supplier = form.save()
            messages.success(request, f"Supplier '{supplier.name}' updated.")
            return redirect(_back_after_edit(back, reverse("supplier_detail", args=[supplier.id]), supplier.id))
        messages.error(request, "Please fix the supplier details below.")

    return render(
        request,
        "pos/add_supplier.html",
        {
            "form": form,
            "supplier": supplier,
            "opening_form": None,
            "next_url": back["back_url"],
            "return_to_purchase": False,
        },
    )
