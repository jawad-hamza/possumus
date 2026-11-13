"""Customers of a distribution business: city, route, order booker and credit limit.

Routes and order bookers are the switchable "field_sales" module. City and credit limit are
plain customer fields and work whenever the customers module is on.

Everything here is a small rule that more than one screen needs:

* how a city is spelled (one spelling per city, so reports group cleanly);
* which order booker a route implies, and which one a sale records;
* where a customer stands against their credit limit, and what the till says about it;
* the city / route / order booker filters of the customer list.
"""
import re
from decimal import Decimal

from django.db.models import Count, Q

from accounts.services.party_balance import get_party_balance

from ..models import Customer, OrderBooker, Route, Supplier, SystemSettings, quantize_money
from .currency import format_money
from .modules import module_enabled

MODULE_KEY = "field_sales"
ZERO = Decimal("0")
# Filter value for "nothing recorded": customers with no city, no route or no order booker.
NONE_VALUE = "none"


def field_sales_enabled():
    """Are routes and order bookers in use in this shop? They hang on customers, so both must be on."""
    return module_enabled(MODULE_KEY) and module_enabled("parties")


# ------------------------------------------------------------------------------- cities

def clean_city(value):
    """A city as typed, with stray spaces removed."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def known_cities():
    """Every city already written on a customer, supplier or route, one spelling each, A to Z."""
    seen = {}
    for model in (Customer, Supplier, Route):
        for city in model.objects.exclude(city="").values_list("city", flat=True).distinct():
            seen.setdefault(city.casefold(), city)
    return sorted(seen.values(), key=str.casefold)


def canonical_city(value):
    """The spelling already on record for this city ("mirpur a.k" becomes "Mirpur A.K")."""
    city = clean_city(value)
    if not city:
        return ""
    for model in (Customer, Route, Supplier):
        match = model.objects.filter(city__iexact=city).values_list("city", flat=True).first()
        if match:
            return match
    return city


# -------------------------------------------------------------- routes and order bookers

def default_order_booker_for_route(route):
    """The one active order booker who covers this route; None when there are none or several."""
    if route is None:
        return None
    bookers = list(route.order_bookers.filter(is_active=True)[:2])
    return bookers[0] if len(bookers) == 1 else None


def get_or_create_route(name, city=""):
    """The route with this name (in this city, when one is given), created when it is new."""
    name = clean_city(name)
    city = canonical_city(city)
    matches = Route.objects.filter(name__iexact=name)
    route = (matches.filter(city__iexact=city).first() if city else None) or matches.first()
    if route is not None:
        return route, False
    return Route.objects.create(name=name, city=city), True


def get_or_create_order_booker(name):
    name = clean_city(name)
    booker = OrderBooker.objects.filter(name__iexact=name).first()
    if booker is not None:
        return booker, False
    return OrderBooker.objects.create(name=name), True


def route_defaults():
    """{route id: {"city": ..., "booker": id or ""}}: what choosing a route fills in on the customer form."""
    defaults = {}
    routes = Route.objects.prefetch_related("order_bookers")
    for route in routes:
        active = [booker for booker in route.order_bookers.all() if booker.is_active]
        defaults[str(route.id)] = {"city": route.city, "booker": str(active[0].id) if len(active) == 1 else ""}
    return defaults


def active_order_bookers():
    return OrderBooker.objects.filter(is_active=True).order_by("name", "id")


def sale_order_booker(request, customer):
    """The order booker a sale being closed at the till is recorded against.

    The till posts ``order_booker_id`` when its order booker box is on screen: that choice
    wins, and an empty choice means "nobody". Without the box the sale takes the booker who
    normally serves the customer. Someone who no longer works here is never recorded.
    """
    if not field_sales_enabled():
        return None
    if "order_booker_id" in request.POST:
        chosen = (request.POST.get("order_booker_id") or "").strip()
        if not chosen.isdigit():
            return None
        return OrderBooker.objects.filter(pk=chosen, is_active=True).first()
    booker = getattr(customer, "order_booker", None)
    return booker if booker is not None and booker.is_active else None


# ------------------------------------------------------------------------- credit limit

def credit_standing(customer, balance=None):
    """Where a customer stands against their credit limit.

    ``balance`` is the ledger balance (positive: they owe us); it is read when not given.
    ``left`` can be more than the limit when the customer has paid in advance.
    """
    limit = customer.credit_limit
    if balance is None:
        balance = get_party_balance(customer)
    balance = quantize_money(balance or 0)
    if limit is None:
        return {"has_limit": False, "limit": None, "balance": balance, "left": None, "over": ZERO, "is_over": False}
    limit = quantize_money(limit)
    left = quantize_money(limit - balance)
    return {
        "has_limit": True,
        "limit": limit,
        "balance": balance,
        "left": max(left, ZERO),
        "over": max(-left, ZERO),
        "is_over": left < 0,
    }


def _balance_words(balance):
    if balance < 0:
        return f"{format_money(-balance)} paid in advance"
    return format_money(balance)


def credit_limit_note(standing):
    """One short line for the customer page and the till: the limit and what is left of it."""
    if not standing["has_limit"]:
        return ""
    if standing["is_over"]:
        return f"Credit limit {format_money(standing['limit'])}, over by {format_money(standing['over'])}"
    return f"Credit limit {format_money(standing['limit'])}, {format_money(standing['left'])} left"


def credit_limit_problem(customer, bill_amount, *, lock=False):
    """"" when this credit bill fits inside the customer's limit, otherwise the facts in one sentence.

    The sentence always states the limit, the current balance and the amount of this bill.
    ``lock`` holds the customer's row until the transaction ends, so two tills closing credit
    sales for the same customer at once cannot both slip under the limit.
    """
    if customer is None or customer.credit_limit is None:
        return ""
    if lock:
        customer = Customer.objects.select_for_update().get(pk=customer.pk)
        if customer.credit_limit is None:
            return ""
    bill = quantize_money(bill_amount or 0)
    standing = credit_standing(customer)
    if standing["balance"] + bill <= standing["limit"]:
        return ""
    over = quantize_money(standing["balance"] + bill - standing["limit"])
    return (
        f"{customer.name} has a credit limit of {format_money(standing['limit'])}. "
        f"Current balance {_balance_words(standing['balance'])}, this bill {format_money(bill)}: "
        f"{format_money(over)} over the limit."
    )


def credit_limit_refuses(system_settings):
    """True when Settings say a sale over the limit is refused (the default); False for warn only."""
    return getattr(system_settings, "credit_limit_action", SystemSettings.CREDIT_LIMIT_REFUSE) != SystemSettings.CREDIT_LIMIT_WARN


# ------------------------------------------------------------------ the till's customer

def till_customer_terms(customer, badge=None):
    """Extra facts the till shows beside the chosen customer: credit limit and usual order booker.

    ``badge`` is the customer's balance badge when the caller already has it (saves a query).
    """
    balance = None
    if badge is not None:
        balance = -badge["amount"] if badge["balance_type"] == "advance" else badge["amount"]
    standing = credit_standing(customer, balance)
    booker_id = ""
    if field_sales_enabled() and customer.order_booker_id and customer.order_booker.is_active:
        booker_id = str(customer.order_booker_id)
    return {
        "credit_note": credit_limit_note(standing),
        "credit_over": standing["is_over"],
        "order_booker_id": booker_id,
    }


# ------------------------------------------------------------ customer list: the filters

def _choice(value, label):
    return {"value": str(value), "label": label}


def customer_area_filters(request):
    """The city / route / order booker choices of the customer list and what is chosen now."""
    chosen_city = clean_city(request.GET.get("city"))
    filters = {
        "city": {
            "name": "city",
            "label": "City",
            "all_label": "All cities",
            "value": chosen_city,
            "options": [
                _choice(city, city)
                for city in sorted(
                    {c.casefold(): c for c in Customer.objects.exclude(city="").values_list("city", flat=True).distinct()}.values(),
                    key=str.casefold,
                )
            ],
        }
    }
    if field_sales_enabled():
        filters["route"] = {
            "name": "route",
            "label": "Route",
            "all_label": "All routes",
            "value": (request.GET.get("route") or "").strip(),
            "options": [
                _choice(route.id, route.label)
                for route in Route.objects.annotate(customer_count=Count("customers")).filter(
                    Q(is_active=True) | Q(customer_count__gt=0)
                )
            ],
        }
        filters["order_booker"] = {
            "name": "order_booker",
            "label": "Order booker",
            "all_label": "All order bookers",
            "value": (request.GET.get("order_booker") or "").strip(),
            "options": [
                _choice(booker.id, booker.name)
                for booker in OrderBooker.objects.annotate(customer_count=Count("customers")).filter(
                    Q(is_active=True) | Q(customer_count__gt=0)
                )
            ],
        }
    for entry in filters.values():
        if entry["options"]:
            entry["options"].append(_choice(NONE_VALUE, f"No {entry['label'].lower()}"))
        entry["active"] = bool(entry["value"])
    return filters


def apply_customer_area_filters(queryset, filters):
    """Narrow a customer queryset to the chosen city, route and order booker."""
    city = filters["city"]["value"]
    if city == NONE_VALUE:
        queryset = queryset.filter(city="")
    elif city:
        queryset = queryset.filter(city__iexact=city)
    for name in ("route", "order_booker"):
        entry = filters.get(name)
        if entry is None or not entry["value"]:
            continue
        if entry["value"] == NONE_VALUE:
            queryset = queryset.filter(**{f"{name}__isnull": True})
        elif entry["value"].isdigit():
            queryset = queryset.filter(**{f"{name}_id": int(entry["value"])})
        else:
            entry["value"] = ""
            entry["active"] = False
    return queryset
