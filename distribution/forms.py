"""Forms of the distribution screens. Order and stock return lines are read by
``services.common.parse_pack_lines``; these are the headers and the master lists."""
from datetime import timedelta

from django import forms
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.models import BankAccount
from pos.customer_form_mixins import RouteChoiceField
from pos.forms import StyledFieldsMixin, StyledModelForm
from pos.models import Customer, OrderBooker, Route, Supplier
from pos.services.field_sales import clean_city as tidy_text

from .models import DeliveryMan, LoadForm, Recovery, StockReturn, Vehicle
from .services.common import routes_in_use


DATE_INPUT = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


def _active_or(model, current_pk=None, **filters):
    query = Q(is_active=True)
    if current_pk:
        query |= Q(pk=current_pk)
    return model.objects.filter(query, **filters)


class CustomerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        where = obj.city or (obj.route.name if obj.route_id else "")
        return f"{obj.name} ({where})" if where else obj.name


# ------------------------------------------------------------------------------ masters
class DeliveryManForm(StyledModelForm):
    class Meta:
        model = DeliveryMan
        fields = ["name", "phone", "is_active"]
        labels = {"is_active": "Working with you"}
        help_texts = {"is_active": "Switch off someone who has left. Past loads and reports keep their name."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_name(self):
        name = tidy_text(self.cleaned_data.get("name"))
        if name and DeliveryMan.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"There is already a delivery man called {name}.")
        return name


class QuickDeliveryManForm(DeliveryManForm):
    class Meta(DeliveryManForm.Meta):
        fields = ["name", "phone"]


class VehicleForm(StyledModelForm):
    class Meta:
        model = Vehicle
        fields = ["name", "number", "is_active"]
        labels = {"name": "Vehicle", "number": "Registration number", "is_active": "In use"}
        help_texts = {
            "name": "What everyone calls it, such as Loader 1 or Shehzore.",
            "is_active": "Switch off a vehicle you no longer run. Past loads keep it.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_name(self):
        return tidy_text(self.cleaned_data.get("name"))

    def clean_number(self):
        number = tidy_text(self.cleaned_data.get("number")).upper()
        if number and Vehicle.objects.filter(number__iexact=number).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"Vehicle {number} is already on the list.")
        return number


class QuickVehicleForm(VehicleForm):
    class Meta(VehicleForm.Meta):
        fields = ["name", "number"]


# ------------------------------------------------------------------------------- orders
class OrderHeaderForm(StyledFieldsMixin, forms.Form):
    customer = CustomerChoiceField(queryset=Customer.objects.none(), label="Customer")
    route = RouteChoiceField(queryset=Route.objects.none(), required=False, label="Route")
    order_booker = forms.ModelChoiceField(queryset=OrderBooker.objects.none(), required=False, label="Order booker")
    order_date = forms.DateField(label="Order date", widget=DATE_INPUT)
    delivery_date = forms.DateField(label="Deliver on", required=False, widget=DATE_INPUT)
    notes = forms.CharField(label="Notes", required=False, max_length=255)

    def __init__(self, *args, order=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customer"].queryset = _active_or(
            Customer, order.customer_id if order else None
        ).select_related("route").order_by("name")
        if routes_in_use():
            self.fields["route"].queryset = _active_or(Route, order.route_id if order else None)
            self.fields["order_booker"].queryset = _active_or(OrderBooker, order.order_booker_id if order else None)
        else:
            del self.fields["route"]
            del self.fields["order_booker"]
        if not self.is_bound and order is None:
            self.initial.setdefault("order_date", timezone.localdate())
        self._apply_bootstrap()
        self.fields["customer"].widget.attrs.update(
            {"data-dist-customer": "", "data-terms-url": reverse("distribution_customer_terms", args=[0])}
        )
        if "route" in self.fields:
            self.fields["route"].widget.attrs["data-dist-route"] = ""
            self.fields["order_booker"].widget.attrs["data-dist-booker"] = ""


class CancelOrderForm(StyledFieldsMixin, forms.Form):
    reason = forms.CharField(label="Why is it cancelled?", max_length=150)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()


# ---------------------------------------------------------------------------- load form
class LoadFormForm(StyledModelForm):
    class Meta:
        model = LoadForm
        fields = ["load_date", "route", "delivery_man", "vehicle", "notes"]
        labels = {"load_date": "Load date", "delivery_man": "Delivery man"}
        widgets = {"load_date": DATE_INPUT}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        if routes_in_use():
            self.fields["route"] = RouteChoiceField(
                queryset=_active_or(Route, instance.route_id), required=False, label="Route"
            )
        else:
            del self.fields["route"]
        self.fields["delivery_man"].queryset = _active_or(DeliveryMan, instance.delivery_man_id)
        self.fields["vehicle"].queryset = _active_or(Vehicle, instance.vehicle_id)
        if not self.is_bound and not instance.pk:
            self.initial.setdefault("load_date", timezone.localdate())
        self._apply_bootstrap()


class CloseLoadForm(StyledFieldsMixin, forms.Form):
    cash_received = forms.DecimalField(
        label="Cash handed in", min_value=0, max_digits=12, decimal_places=2,
        help_text="Count the cash the delivery man hands over. Any difference from the cash expected is posted to the Cash Short and Over account when the load is closed.",
    )
    notes = forms.CharField(label="Notes", required=False, max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()
        self.fields["cash_received"].widget.attrs.update({"inputmode": "decimal", "step": "0.01"})


# ------------------------------------------------------------------------ stock return
class StockReturnHeaderForm(StyledFieldsMixin, forms.Form):
    customer = CustomerChoiceField(queryset=Customer.objects.none(), required=False, label="Customer")
    supplier = forms.ModelChoiceField(queryset=Supplier.objects.none(), required=False, label="Supplier")
    return_date = forms.DateField(label="Date", widget=DATE_INPUT)
    settlement = forms.ChoiceField(label="Settle by", choices=StockReturn.SETTLEMENT_CHOICES)
    bank_account = forms.ModelChoiceField(queryset=BankAccount.objects.none(), required=False, label="Bank account")
    load_form = forms.ModelChoiceField(queryset=LoadForm.objects.none(), required=False, label="Came back on load")
    reason = forms.CharField(label="Reason", max_length=150)
    notes = forms.CharField(label="Notes", required=False, max_length=255)

    def __init__(self, *args, kind=StockReturn.FROM_CUSTOMER, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        if kind == StockReturn.FROM_CUSTOMER:
            del self.fields["supplier"]
            self.fields["customer"].queryset = Customer.objects.filter(is_active=True).select_related("route").order_by("name")
            self.fields["settlement"].choices = (
                (StockReturn.ON_ACCOUNT, "Credit the customer's account"),
                (StockReturn.CASH, "Pay back in cash"),
                (StockReturn.BANK, "Pay back by bank"),
            )
            recent = timezone.localdate() - timedelta(days=45)
            self.fields["load_form"].queryset = (
                LoadForm.objects.exclude(status=LoadForm.OPEN).filter(load_date__gte=recent).select_related("route").order_by("-load_date", "-id")
            )
            self.fields["load_form"].label_from_instance = lambda load: f"{load.display_number}, {load.load_date:%d %b}" + (
                f", {load.route.name}" if load.route_id else ""
            )
        else:
            del self.fields["customer"]
            del self.fields["load_form"]
            self.fields["supplier"].queryset = Supplier.objects.order_by("name")
            self.fields["settlement"].choices = (
                (StockReturn.ON_ACCOUNT, "Reduce what we owe them"),
                (StockReturn.CASH, "They pay back in cash"),
                (StockReturn.BANK, "They pay back by bank"),
            )
        self.fields["bank_account"].queryset = BankAccount.objects.filter(is_active=True).order_by("name")
        if not self.is_bound:
            self.initial.setdefault("return_date", timezone.localdate())
        self._apply_bootstrap()
        self.fields["settlement"].widget.attrs["data-dist-settlement"] = ""

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("settlement") == StockReturn.BANK and not cleaned.get("bank_account"):
            self.add_error("bank_account", "Choose the bank account.")
        return cleaned


# ---------------------------------------------------------------------------- recovery
class RecoveryFilterForm(StyledFieldsMixin, forms.Form):
    route = RouteChoiceField(queryset=Route.objects.none(), required=False, label="Route")
    order_booker = forms.ModelChoiceField(queryset=OrderBooker.objects.none(), required=False, label="Order booker")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if routes_in_use():
            self.fields["route"].queryset = Route.objects.filter(is_active=True)
            self.fields["order_booker"].queryset = OrderBooker.objects.filter(is_active=True)
        else:
            del self.fields["route"]
            del self.fields["order_booker"]
        self._apply_bootstrap()


COLLECTOR_CHOICES = Recovery.COLLECTOR_CHOICES
