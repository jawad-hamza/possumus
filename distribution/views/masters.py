"""Delivery men and vehicles: two short master lists, an add and an edit page each, and the
JSON endpoints behind the "Add delivery man" / "Add vehicle" side panels."""
from django.contrib import messages
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.services.audit import record_audit
from pos.services.access import ACCESS_MANAGE
from pos.services.navigation import return_context, with_touched_row

from ..forms import DeliveryManForm, QuickDeliveryManForm, QuickVehicleForm, VehicleForm
from ..models import DeliveryMan, Vehicle
from .common import distribution_access, json_form_errors, tabs_context

MASTERS = {
    "delivery_man": {
        "model": DeliveryMan,
        "form": DeliveryManForm,
        "quick_form": QuickDeliveryManForm,
        "prefix": "quick_delivery_man",
        "list": "distribution_delivery_man_list",
        "add": "distribution_delivery_man_add",
        "edit": "distribution_delivery_man_edit",
        "tab": "delivery_men",
        "singular": "delivery man",
        "plural": "Delivery men",
        "help": "The people who take loads out, hand over the goods and bring back cash and returns. A name for loads and reports, not a login.",
        "icon": "bi-person-walking",
    },
    "vehicle": {
        "model": Vehicle,
        "form": VehicleForm,
        "quick_form": QuickVehicleForm,
        "prefix": "quick_vehicle",
        "list": "distribution_vehicle_list",
        "add": "distribution_vehicle_add",
        "edit": "distribution_vehicle_edit",
        "tab": "vehicles",
        "singular": "vehicle",
        "plural": "Vehicles",
        "help": "The loaders and vans a load goes out on.",
        "icon": "bi-truck",
    },
}


def _list(request, kind):
    spec = MASTERS[kind]
    rows = list(spec["model"].objects.annotate(load_count=Count("load_forms")))
    return render(
        request,
        "distribution/master_list.html",
        {
            "spec": spec,
            "kind": kind,
            "rows": rows,
            "active_count": sum(1 for row in rows if row.is_active),
            **tabs_context(spec["tab"]),
        },
    )


def _form(request, kind, pk=None):
    spec = MASTERS[kind]
    instance = get_object_or_404(spec["model"], pk=pk) if pk else None
    form = spec["form"](request.POST or None, instance=instance)
    back = return_context(request, reverse(spec["list"]))
    if request.method == "POST":
        if form.is_valid():
            saved = form.save()
            record_audit(
                action=f"{kind}_{'changed' if instance else 'added'}",
                description=f"{spec['singular'].capitalize()} '{saved}' {'updated' if instance else 'added'}.",
                actor=request.user,
                target=saved,
            )
            messages.success(request, f"{spec['singular'].capitalize()} '{saved}' {'updated' if instance else 'added'}.")
            return redirect(with_touched_row(back["back_url"], saved.pk))
        messages.error(request, f"Please fix the {spec['singular']} details below.")
    return render(
        request,
        "distribution/master_form.html",
        {"spec": spec, "kind": kind, "form": form, "instance": instance, **back, **tabs_context(spec["tab"])},
    )


def _quick(request, kind):
    spec = MASTERS[kind]
    form = spec["quick_form"](request.POST, prefix=spec["prefix"])
    if form.is_valid():
        saved = form.save()
        record_audit(action=f"{kind}_added", description=f"{spec['singular'].capitalize()} '{saved}' added.", actor=request.user, target=saved)
        return JsonResponse({"status": "ok", kind: {"id": saved.id, "name": str(saved)}})
    return JsonResponse({"status": "error", "errors": json_form_errors(form)}, status=400)


@distribution_access()
def delivery_man_list(request):
    return _list(request, "delivery_man")


@distribution_access(ACCESS_MANAGE)
def delivery_man_add(request):
    return _form(request, "delivery_man")


@distribution_access(ACCESS_MANAGE)
def delivery_man_edit(request, pk):
    return _form(request, "delivery_man", pk)


@distribution_access(ACCESS_MANAGE)
@require_POST
def quick_delivery_man(request):
    return _quick(request, "delivery_man")


@distribution_access()
def vehicle_list(request):
    return _list(request, "vehicle")


@distribution_access(ACCESS_MANAGE)
def vehicle_add(request):
    return _form(request, "vehicle")


@distribution_access(ACCESS_MANAGE)
def vehicle_edit(request, pk):
    return _form(request, "vehicle", pk)


@distribution_access(ACCESS_MANAGE)
@require_POST
def quick_vehicle(request):
    return _quick(request, "vehicle")


@distribution_access(ACCESS_MANAGE)
@require_POST
def quick_customer(request):
    """A new shop added from the order form without losing the order being typed."""
    from pos.forms import CustomerForm
    from pos.services.access import user_can_access_module

    if not user_can_access_module(request.user, "parties", ACCESS_MANAGE):
        return JsonResponse({"status": "error", "message": "Your role cannot add customers."}, status=403)
    form = CustomerForm(request.POST, prefix="quick_customer")
    if form.is_valid():
        customer = form.save()
        record_audit(action="customer_added", description=f"Customer '{customer.name}' added from a sales order.", actor=request.user, target=customer)
        return JsonResponse({"status": "ok", "customer": {"id": customer.id, "name": customer.name}})
    return JsonResponse({"status": "error", "errors": json_form_errors(form)}, status=400)
