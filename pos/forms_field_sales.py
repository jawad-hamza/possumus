"""Forms of the routes and order bookers lists (the "field_sales" module)."""
from django import forms

from .customer_form_mixins import PartyCityMixin, RouteChoiceField
from .forms import StyledModelForm
from .models import OrderBooker, Route
from .services.field_sales import clean_city as tidy_text


class RouteForm(PartyCityMixin, StyledModelForm):
    class Meta:
        model = Route
        fields = ["name", "city", "is_active"]
        labels = {"name": "Route name", "is_active": "In use"}
        help_texts = {
            "city": "Customers put on this route take this city when they have none.",
            "is_active": "Switch off a route you no longer visit. Its customers and past sales keep it.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_name(self):
        return tidy_text(self.cleaned_data.get("name"))

    def clean(self):
        cleaned = super().clean()
        name, city = cleaned.get("name"), cleaned.get("city") or ""
        if name:
            same = Route.objects.filter(name__iexact=name, city__iexact=city).exclude(pk=self.instance.pk)
            if same.exists():
                where = f" in {city}" if city else ""
                self.add_error("name", f"There is already a route called {name}{where}.")
        return cleaned


class QuickRouteForm(RouteForm):
    """The side-panel form: a route added without leaving the customer being typed."""

    class Meta(RouteForm.Meta):
        fields = ["name", "city"]


class RouteMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.label


class OrderBookerForm(StyledModelForm):
    class Meta:
        model = OrderBooker
        fields = ["name", "phone", "is_active", "routes"]
        labels = {"is_active": "Working with you"}
        help_texts = {
            "is_active": "Switch off someone who has left. Past sales and reports keep their name.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "routes" in self.fields:
            covered = self.instance.routes.values("pk") if self.instance.pk else Route.objects.none().values("pk")
            self.fields["routes"] = RouteMultipleChoiceField(
                queryset=(Route.objects.filter(is_active=True) | Route.objects.filter(pk__in=covered)).distinct(),
                required=False,
                label="Routes covered",
                widget=forms.CheckboxSelectMultiple,
                help_text="A customer put on a route that only one order booker covers gets that order booker.",
            )
        self._apply_bootstrap()
        if "routes" in self.fields:
            self.fields["routes"].widget.attrs["class"] = "form-check-input"

    def clean_name(self):
        name = tidy_text(self.cleaned_data.get("name"))
        if name and OrderBooker.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"There is already an order booker called {name}. Add a surname or an area to tell them apart.")
        return name


class QuickOrderBookerForm(OrderBookerForm):
    """The side-panel form: an order booker added without leaving the customer being typed."""

    class Meta(OrderBookerForm.Meta):
        fields = ["name", "phone"]


__all__ = [
    "OrderBookerForm",
    "QuickOrderBookerForm",
    "QuickRouteForm",
    "RouteChoiceField",
    "RouteForm",
]
