"""What the customer and supplier forms gained for a distribution business.

pos/forms.py mixes these into CustomerForm and SupplierForm, so the full add and edit pages,
the quick add at the till and the side-panel forms all behave the same way:

* city: free text with the cities already on record offered as you type, one spelling each;
* credit limit: optional, empty means no limit;
* route and order booker (only while the "field_sales" module is on): chosen from the lists,
  or typed as a new one in place. Choosing a route fills the order booker when exactly one
  covers it, and the city when the customer has none.

This module must not import pos.forms (pos.forms imports it).
"""
from django import forms

from accounts.services.current_actor import get_current_actor

from .models import OrderBooker, Route
from .services.access import ACCESS_MANAGE, user_can_access_module
from .services.field_sales import (
    canonical_city,
    clean_city as tidy_text,
    default_order_booker_for_route,
    field_sales_enabled,
    get_or_create_order_booker,
    get_or_create_route,
    known_cities,
)

CITY_LIST_ID = "party-city-options"


class RouteChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.label


def can_add_field_sales_records(user=None):
    """Routes and order bookers are master data: only someone who manages customers may add one."""
    user = user or get_current_actor()
    return bool(user is not None and field_sales_enabled() and user_can_access_module(user, "parties", ACCESS_MANAGE))


class PartyCityMixin:
    """A city field that suggests the cities already in use and keeps one spelling of each."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        city = self.fields.get("city")
        if city is not None:
            city.widget.attrs.update({"list": CITY_LIST_ID, "autocomplete": "off"})

    @property
    def city_options(self):
        return known_cities()

    def clean_city(self):
        return canonical_city(self.cleaned_data.get("city"))


class CustomerTermsMixin(PartyCityMixin):
    """City, route, order booker and credit limit on the customer form."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        limit = self.fields.get("credit_limit")
        if limit is not None:
            limit.label = "Credit limit"
            limit.help_text = "The most this customer may owe you. Leave empty for no limit."
            limit.widget.attrs.update({"min": "0", "step": "0.01", "inputmode": "decimal", "placeholder": "No limit"})

        self.uses_field_sales = field_sales_enabled()
        self.can_add_field_sales = self.uses_field_sales and can_add_field_sales_records()
        if not self.uses_field_sales:
            # The owner switched routes and order bookers off: what is stored stays, nothing shows.
            self.fields.pop("route", None)
            self.fields.pop("order_booker", None)
            return

        current_route = self.instance.route_id if self.instance.pk else None
        current_booker = self.instance.order_booker_id if self.instance.pk else None
        # Someone retired stays selectable on the customers who still carry them.
        self.fields["route"] = RouteChoiceField(
            queryset=(Route.objects.filter(is_active=True) | Route.objects.filter(pk=current_route)).distinct(),
            required=False,
            label="Route",
        )
        self.fields["order_booker"] = forms.ModelChoiceField(
            queryset=(OrderBooker.objects.filter(is_active=True) | OrderBooker.objects.filter(pk=current_booker)).distinct(),
            required=False,
            label="Order booker",
            help_text="Who normally takes this customer's orders. Left empty, it follows the route when one order booker covers it.",
        )
        if self.can_add_field_sales:
            # Typed in place where a side panel cannot open (the till's own customer panel).
            self.fields["new_route"] = forms.CharField(required=False, max_length=100, label="New route")
            self.fields["new_order_booker"] = forms.CharField(required=False, max_length=100, label="New order booker")
        # A fixed order whatever the module state: the till's panel lays the fields out in this order.
        self.order_fields(
            ["name", "phone", "email", "address", "city", "route", "new_route", "order_booker", "new_order_booker", "credit_limit"]
        )

    def clean_credit_limit(self):
        limit = self.cleaned_data.get("credit_limit")
        if limit is not None and limit < 0:
            raise forms.ValidationError("The credit limit cannot be negative. Leave it empty for no limit.")
        return limit

    def clean_new_route(self):
        return tidy_text(self.cleaned_data.get("new_route"))

    def clean_new_order_booker(self):
        return tidy_text(self.cleaned_data.get("new_order_booker"))

    def _route_changed(self):
        return not self.instance.pk or "route" in self.changed_data or bool(self.cleaned_data.get("new_route"))

    def save(self, commit=True):
        customer = super().save(commit=False)
        if self.uses_field_sales:
            route_changed = self._route_changed()
            new_route = self.cleaned_data.get("new_route")
            if new_route:
                customer.route, _created = get_or_create_route(new_route, customer.city)
            new_booker = self.cleaned_data.get("new_order_booker")
            if new_booker:
                customer.order_booker, created = get_or_create_order_booker(new_booker)
                if created and customer.route_id:
                    customer.order_booker.routes.add(customer.route)
            if customer.route_id and route_changed:
                if not customer.order_booker_id:
                    customer.order_booker = default_order_booker_for_route(customer.route)
                if not customer.city and customer.route.city:
                    customer.city = customer.route.city
        if commit:
            customer.save()
            self.save_m2m()
        return customer
