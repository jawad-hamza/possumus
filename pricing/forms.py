"""Forms of the price list and scheme screens."""
from decimal import Decimal

from django import forms
from django.db.models import Q

from pos.customer_form_mixins import RouteChoiceField
from pos.forms import StyledFieldsMixin, StyledModelForm
from pos.models import PACK_LEVEL_CHOICES, Category, Customer, Product, Route, quantize_money
from pos.services.field_sales import field_sales_enabled

from .models import PriceList, Scheme

DATE_INPUT = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ProductChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.display_name} ({obj.sku})" if obj.sku else obj.display_name


class CustomerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        where = obj.city or (obj.route.name if obj.route_id else "")
        return f"{obj.name} ({where})" if where else obj.name


def _products(current=None):
    query = Q(is_active=True)
    if current:
        query |= Q(pk=current)
    return Product.objects.filter(query).order_by("name", "variant_label")


# --------------------------------------------------------------------------- price lists
class PriceListForm(StyledModelForm):
    class Meta:
        model = PriceList
        fields = ["name", "is_active", "notes"]
        labels = {"name": "Name", "is_active": "In use", "notes": "Notes"}
        help_texts = {
            "name": "Such as Wholesale, Retailers or Karachi route.",
            "is_active": "Switched off, its customers buy at the product price until it is on again.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_bootstrap()

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if PriceList.objects.filter(name__iexact=name).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"There is already a price list called {name}.")
        return name


class PriceListItemForm(StyledFieldsMixin, forms.Form):
    """One product's price on a list, typed per the largest pack the product has."""

    product = ProductChoiceField(queryset=Product.objects.none(), label="Product")
    price = forms.DecimalField(label="Trade price", min_value=Decimal("0.01"), max_digits=12, decimal_places=2,
                               help_text="Per carton, or the largest pack the product has.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = _products()
        self._apply_bootstrap()
        self.fields["price"].widget.attrs.update({"step": "0.01", "inputmode": "decimal"})

    def clean(self):
        cleaned = super().clean()
        product, price = cleaned.get("product"), cleaned.get("price")
        if product is None or price is None:
            return cleaned
        top = product.top_pack_level
        minimum = product.minimum_sale_price_for_cost(product.pack_cost(top))
        if price < minimum:
            self.add_error(
                "price",
                f"{product.display_name} cannot go below {minimum} a {product.pack_label(top)} "
                "to keep its minimum margin.",
            )
        return cleaned


class FillPriceListForm(StyledFieldsMixin, forms.Form):
    """Put many products on a list at once: their own price, less or more a percentage."""

    category = forms.ModelChoiceField(queryset=Category.objects.none(), required=False, label="Category",
                                      empty_label="Every category")
    adjust_percent = forms.DecimalField(label="Change from the product price (%)", max_digits=6, decimal_places=2,
                                        initial=Decimal("0"),
                                        help_text="-2 sells 2% under the product price, 3 sells 3% above it.")
    replace = forms.BooleanField(label="Also change products already on this list", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self._apply_bootstrap()
        self.fields["adjust_percent"].widget.attrs.update({"step": "0.01", "inputmode": "decimal"})

    def clean_adjust_percent(self):
        value = self.cleaned_data.get("adjust_percent") or Decimal("0")
        if value <= Decimal("-100"):
            raise forms.ValidationError("A price list cannot take the whole price off.")
        return value


class CustomerMultipleField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return CustomerChoiceField.label_from_instance(self, obj)


class RouteMultipleField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        return obj.label


class AssignPriceListForm(StyledFieldsMixin, forms.Form):
    customers = CustomerMultipleField(queryset=Customer.objects.none(), required=False, label="Customers")
    routes = RouteMultipleField(queryset=Route.objects.none(), required=False, label="Routes (their default)")

    def __init__(self, *args, price_list=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["customers"].queryset = Customer.objects.filter(
            Q(is_active=True) | Q(price_list=price_list)
        ).select_related("route").order_by("name")
        if field_sales_enabled():
            self.fields["routes"].queryset = Route.objects.filter(Q(is_active=True) | Q(price_list=price_list)).order_by("name")
        else:
            del self.fields["routes"]
        if price_list is not None and not self.is_bound:
            self.initial["customers"] = list(price_list.customers.values_list("pk", flat=True))
            if "routes" in self.fields:
                self.initial["routes"] = list(price_list.routes.values_list("pk", flat=True))
        self._apply_bootstrap()
        for name in ("customers", "routes"):
            if name in self.fields:
                self.fields[name].widget.attrs.update({"size": "10"})


# ------------------------------------------------------------------------------- schemes
class SchemeForm(StyledModelForm):
    class Meta:
        model = Scheme
        fields = [
            "name",
            "is_active",
            "start_date",
            "end_date",
            "scope",
            "route",
            "customer",
            "product",
            "category",
            "trigger_quantity",
            "trigger_unit",
            "reward",
            "free_product",
            "free_quantity",
            "free_unit",
            "discount_percent",
            "discount_amount",
            "notes",
        ]
        labels = {
            "is_active": "Running",
            "start_date": "Starts",
            "end_date": "Ends",
            "scope": "For",
            "product": "Product",
            "category": "Or every product in category",
            "trigger_quantity": "When a line reaches",
            "trigger_unit": "Unit",
            "reward": "The customer gets",
            "free_product": "Free product",
            "free_quantity": "Free quantity",
            "free_unit": "Free unit",
            "discount_percent": "Discount (%)",
            "discount_amount": "Amount off each unit",
        }
        help_texts = {
            "start_date": "Leave empty to start now.",
            "end_date": "Leave empty to run until it is switched off.",
            "trigger_quantity": "Free goods repeat: 10 + 1 gives 2 free on 20.",
            "free_product": "Leave empty for the same product.",
            "discount_amount": "Taken off each unit of the trigger unit, such as 50 off a carton.",
        }
        widgets = {"start_date": DATE_INPUT, "end_date": DATE_INPUT}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        self.fields["product"] = ProductChoiceField(queryset=_products(instance.product_id), required=False, label="Product")
        self.fields["free_product"] = ProductChoiceField(
            queryset=_products(instance.free_product_id), required=False, label="Free product",
            help_text="Leave empty for the same product.",
        )
        self.fields["category"].queryset = Category.objects.filter(Q(is_active=True) | Q(pk=instance.category_id)).order_by("name")
        self.fields["category"].required = False
        self.fields["customer"] = CustomerChoiceField(
            queryset=Customer.objects.filter(Q(is_active=True) | Q(pk=instance.customer_id)).select_related("route").order_by("name"),
            required=False,
            label="Customer",
        )
        if field_sales_enabled() or instance.route_id:
            self.fields["route"] = RouteChoiceField(
                queryset=Route.objects.filter(Q(is_active=True) | Q(pk=instance.route_id)), required=False, label="Route"
            )
        else:
            del self.fields["route"]
            self.fields["scope"].choices = [choice for choice in Scheme.SCOPE_CHOICES if choice[0] != Scheme.SCOPE_ROUTE]
        for name in ("trigger_unit", "free_unit"):
            self.fields[name].choices = list(PACK_LEVEL_CHOICES)
        self._apply_bootstrap()
        for name in ("trigger_quantity", "free_quantity"):
            self.fields[name].widget.attrs.update({"min": "0", "step": "1", "inputmode": "decimal"})
        for name in ("discount_percent", "discount_amount"):
            self.fields[name].widget.attrs.update({"min": "0", "step": "0.01", "inputmode": "decimal"})
        self.fields["scope"].widget.attrs["data-scheme-scope"] = ""
        self.fields["reward"].widget.attrs["data-scheme-reward"] = ""

    def clean_discount_amount(self):
        value = self.cleaned_data.get("discount_amount")
        return quantize_money(value) if value is not None else None
