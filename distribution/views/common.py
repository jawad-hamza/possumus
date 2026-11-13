"""What every distribution page shares: the access gate, the tabs and the side-panel forms."""
from functools import wraps

from django.http import JsonResponse
from django.urls import reverse

from pos.services.access import ACCESS_MANAGE, ACCESS_VIEW, feature_required, module_access_required, user_can_access_module
from pos.services.reporting import parse_report_date

ACCESS_KEY = "distribution"

TABS = (
    ("orders", "Orders", "distribution_order_list"),
    ("loads", "Loads", "distribution_load_list"),
    ("returns", "Stock returns", "distribution_return_list"),
    ("recovery", "Recovery", "distribution_recovery"),
    ("delivery_men", "Delivery men", "distribution_delivery_man_list"),
    ("vehicles", "Vehicles", "distribution_vehicle_list"),
)


def distribution_access(minimum=ACCESS_VIEW):
    """The Distribution access area, the Distribution module and the customers module it stands on."""

    def decorator(view_func):
        gated = module_access_required(ACCESS_KEY, minimum=minimum)(feature_required("parties")(view_func))

        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            return gated(request, *args, **kwargs)

        return wrapped

    return decorator


def can_manage(user):
    return user_can_access_module(user, ACCESS_KEY, ACCESS_MANAGE)


def tabs_context(active):
    return {
        "dist_tabs": [{"key": key, "label": label, "url": reverse(name), "active": key == active} for key, label, name in TABS],
        "dist_active": active,
    }


def json_form_errors(form):
    return {field: [entry["message"] for entry in entries] for field, entries in form.errors.get_json_data().items()}


def json_error(message, status=400):
    return JsonResponse({"status": "error", "message": message}, status=status)


def quick_panels(request, *kinds):
    """Side-panel forms (return-flow.js) for adding a delivery man or a vehicle in place."""
    from pos.forms import CustomerForm

    from ..forms import QuickDeliveryManForm, QuickVehicleForm

    if not can_manage(request.user):
        return {"quick_add": [], "quick_add_kinds": {}}
    if "customer" in kinds and not user_can_access_module(request.user, "parties", ACCESS_MANAGE):
        kinds = tuple(kind for kind in kinds if kind != "customer")
    specs = {
        "customer": ("Add customer", "Save customer", "distribution_quick_customer", lambda: CustomerForm(prefix="quick_customer")),
        "delivery_man": ("Add delivery man", "Save delivery man", "distribution_quick_delivery_man", lambda: QuickDeliveryManForm(prefix="quick_delivery_man")),
        "vehicle": ("Add vehicle", "Save vehicle", "distribution_quick_vehicle", lambda: QuickVehicleForm(prefix="quick_vehicle")),
    }
    panels = []
    for kind in kinds:
        title, save, url_name, make = specs[kind]
        panels.append({"kind": kind, "title": title, "save_label": save, "url": reverse(url_name), "form": make()})
    return {"quick_add": panels, "quick_add_kinds": {panel["kind"]: True for panel in panels}}


def date_param(request, name, default=None):
    return parse_report_date(request.GET.get(name), default)


def pk_param(request, name, source=None):
    value = (source if source is not None else request.GET).get(name, "")
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None
