"""Price lists and schemes: lists, add and edit pages. Both live under Products in the sidebar
and follow the Products access area; each is gated by its own feature module."""
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.services.audit import record_audit
from pos.models import Customer, Product, Route, quantize_money
from pos.services.access import ACCESS_MANAGE, ACCESS_VIEW, feature_required, module_access_required, user_can_access_module
from pos.services.navigation import return_context, safe_next_url, with_touched_row

from .forms import AssignPriceListForm, FillPriceListForm, PriceListForm, PriceListItemForm, SchemeForm
from .models import PriceList, PriceListItem, Scheme

ACCESS_KEY = "products"
TABS = (
    ("price_lists", "Price lists", "pricing_price_list_list", "price_lists"),
    ("schemes", "Schemes", "pricing_scheme_list", "schemes"),
)


def pricing_access(feature, minimum=ACCESS_VIEW):
    """The Products access area and the feature module of the screen."""

    def decorator(view_func):
        gated = module_access_required(ACCESS_KEY, minimum=minimum)(feature_required(feature)(view_func))

        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            return gated(request, *args, **kwargs)

        return wrapped

    return decorator


def _tabs(active):
    from pos.services.modules import module_enabled

    return {
        "pricing_tabs": [
            {"key": key, "label": label, "url": reverse(name), "active": key == active}
            for key, label, name, module in TABS
            if module_enabled(module)
        ],
        "can_manage": False,
    }


def _context(request, active, **extra):
    context = _tabs(active)
    context["can_manage"] = user_can_access_module(request.user, ACCESS_KEY, ACCESS_MANAGE)
    context.update(extra)
    return context


# --------------------------------------------------------------------------- price lists
@pricing_access("price_lists")
def price_list_list(request):
    lists = PriceList.objects.annotate(
        item_count=Count("items", distinct=True),
        customer_count=Count("customers", distinct=True),
        route_count=Count("routes", distinct=True),
    ).order_by("-is_active", "name")
    return render(request, "pricing/price_list_list.html", _context(request, "price_lists", lists=list(lists)))


def _price_list_form(request, instance=None):
    form = PriceListForm(request.POST or None, instance=instance)
    back = return_context(request, reverse("pricing_price_list_list"))
    if request.method == "POST":
        if form.is_valid():
            saved = form.save()
            record_audit(
                action=f"price_list_{'changed' if instance else 'added'}",
                description=f"Price list '{saved.name}' {'updated' if instance else 'added'}.",
                actor=request.user,
                target=saved,
            )
            messages.success(request, f"Price list '{saved.name}' {'updated' if instance else 'added'}.")
            if instance is None:
                return redirect("pricing_price_list_detail", pk=saved.pk)
            return redirect(with_touched_row(back["back_url"], saved.pk))
        messages.error(request, "Please fix the price list details below.")
    return render(
        request,
        "pricing/price_list_form.html",
        _context(request, "price_lists", form=form, instance=instance, **back),
    )


@pricing_access("price_lists", ACCESS_MANAGE)
def price_list_add(request):
    return _price_list_form(request)


@pricing_access("price_lists", ACCESS_MANAGE)
def price_list_edit(request, pk):
    return _price_list_form(request, get_object_or_404(PriceList, pk=pk))


def _item_rows(price_list):
    rows = []
    items = price_list.items.select_related("product", "product__category").order_by("product__name", "product__variant_label")
    for item in items:
        product = item.product
        top = product.top_pack_level
        list_price = item.pack_price(top)
        product_price = product.pack_price(top)
        cost = product.pack_cost(top)
        rows.append(
            {
                "item": item,
                "product": product,
                "unit": product.pack_label(top),
                "list_price": list_price,
                "product_price": product_price,
                "difference": quantize_money(list_price - product_price),
                "difference_percent": quantize_money((list_price - product_price) / product_price * 100) if product_price else None,
                "margin_percent": quantize_money((list_price - cost) / cost * 100) if cost else None,
                "below_minimum": list_price < product.minimum_sale_price_for_cost(cost),
                "retail_price": product.pack_retail_price(top),
            }
        )
    return rows


def _set_price(price_list, product, pack_price, user):
    factor = product.pack_factor(product.top_pack_level)
    base = product.quantize_unit_money(Decimal(pack_price) / factor)
    item, created = PriceListItem.objects.update_or_create(price_list=price_list, product=product, defaults={"price": base})
    record_audit(
        action="price_list_price_set",
        description=f"{product.display_name} at {quantize_money(pack_price)} on price list '{price_list.name}'.",
        actor=user,
        target=price_list,
    )
    return item, created


def _fill(price_list, data, user):
    """Every active product (of a category) at its own price, changed by a percentage. Returns how many."""
    products = Product.objects.filter(is_active=True)
    if data.get("category"):
        products = products.filter(category=data["category"])
    existing = set(price_list.items.values_list("product_id", flat=True))
    factor = Decimal("1") + Decimal(data.get("adjust_percent") or 0) / Decimal("100")
    added = changed = skipped = 0
    for product in products:
        if product.pk in existing and not data.get("replace"):
            continue
        top = product.top_pack_level
        pack_price = quantize_money(product.pack_price(top) * factor)
        if pack_price <= 0 or pack_price < product.minimum_sale_price_for_cost(product.pack_cost(top)):
            skipped += 1
            continue
        base = product.quantize_unit_money(pack_price / product.pack_factor(top))
        _, created = PriceListItem.objects.update_or_create(price_list=price_list, product=product, defaults={"price": base})
        if created:
            added += 1
        else:
            changed += 1
    record_audit(
        action="price_list_filled",
        description=f"Price list '{price_list.name}' filled from product prices: {added} added, {changed} changed.",
        actor=user,
        target=price_list,
        metadata={"adjust_percent": str(data.get("adjust_percent") or 0), "skipped": skipped},
    )
    return added, changed, skipped


def _assign(price_list, data, user):
    customers = set(customer.pk for customer in data.get("customers") or [])
    with transaction.atomic():
        Customer.objects.filter(price_list=price_list).exclude(pk__in=customers).update(price_list=None)
        Customer.objects.filter(pk__in=customers).update(price_list=price_list)
        if "routes" in data:
            routes = set(route.pk for route in data.get("routes") or [])
            Route.objects.filter(price_list=price_list).exclude(pk__in=routes).update(price_list=None)
            Route.objects.filter(pk__in=routes).update(price_list=price_list)
    record_audit(
        action="price_list_assigned",
        description=f"Price list '{price_list.name}' given to {len(customers)} customer(s).",
        actor=user,
        target=price_list,
    )


@pricing_access("price_lists")
def price_list_detail(request, pk):
    price_list = get_object_or_404(PriceList, pk=pk)
    item_form = PriceListItemForm(prefix="item")
    fill_form = FillPriceListForm(prefix="fill")
    assign_form = AssignPriceListForm(price_list=price_list, prefix="assign")
    if request.method == "POST":
        action = request.POST.get("action")
        url = reverse("pricing_price_list_detail", args=[price_list.pk])
        if action == "set_price":
            item_form = PriceListItemForm(request.POST, prefix="item")
            if item_form.is_valid():
                product, price = item_form.cleaned_data["product"], item_form.cleaned_data["price"]
                _, created = _set_price(price_list, product, price, request.user)
                messages.success(request, f"{product.display_name} {'added at' if created else 'now'} {quantize_money(price)} a {product.pack_label(product.top_pack_level)}.")
                return redirect(with_touched_row(url, product.pk))
            messages.error(request, "Please fix the price below.")
        elif action == "remove":
            item = PriceListItem.objects.filter(price_list=price_list, pk=request.POST.get("item")).select_related("product").first()
            if item is not None:
                name = item.product.display_name
                item.delete()
                record_audit(action="price_list_price_removed", description=f"{name} taken off price list '{price_list.name}'.", actor=request.user, target=price_list)
                messages.success(request, f"{name} taken off the list. It sells at its own price to these customers.")
            return redirect(url)
        elif action == "fill":
            fill_form = FillPriceListForm(request.POST, prefix="fill")
            if fill_form.is_valid():
                added, changed, skipped = _fill(price_list, fill_form.cleaned_data, request.user)
                text = f"{added} product{'s' if added != 1 else ''} added, {changed} changed."
                if skipped:
                    text += f" {skipped} left out: the change would take them below their minimum margin."
                messages.success(request, text)
                return redirect(url)
            messages.error(request, "Please fix the figures below.")
        elif action == "assign":
            assign_form = AssignPriceListForm(request.POST, price_list=price_list, prefix="assign")
            if assign_form.is_valid():
                _assign(price_list, assign_form.cleaned_data, request.user)
                messages.success(request, f"Customers and routes of '{price_list.name}' saved.")
                return redirect(url)
            messages.error(request, "Please fix the choices below.")
    return render(
        request,
        "pricing/price_list_detail.html",
        _context(
            request,
            "price_lists",
            price_list=price_list,
            rows=_item_rows(price_list),
            item_form=item_form,
            fill_form=fill_form,
            assign_form=assign_form,
            customers=list(price_list.customers.order_by("name")),
            routes=list(price_list.routes.order_by("name")),
        ),
    )


# ------------------------------------------------------------------------------- schemes
SCHEME_FILTERS = (("running", "Running"), ("all", "All"), ("off", "Switched off or ended"))


@pricing_access("schemes")
def scheme_list(request):
    from django.utils import timezone

    show = request.GET.get("show") or "running"
    if show not in dict(SCHEME_FILTERS):
        show = "running"
    today = timezone.localdate()
    running = Q(is_active=True) & (Q(start_date__isnull=True) | Q(start_date__lte=today)) & (Q(end_date__isnull=True) | Q(end_date__gte=today))
    schemes = Scheme.objects.select_related("product", "category", "free_product", "route", "customer").annotate(
        uses=Count("sale_items", distinct=True)
    )
    if show == "running":
        schemes = schemes.filter(running)
    elif show == "off":
        schemes = schemes.exclude(running)
    return render(
        request,
        "pricing/scheme_list.html",
        _context(
            request,
            "schemes",
            schemes=list(schemes.order_by("-is_active", "name", "id")),
            show=show,
            filters=SCHEME_FILTERS,
        ),
    )


def _scheme_form(request, instance=None):
    form = SchemeForm(request.POST or None, instance=instance)
    back = return_context(request, reverse("pricing_scheme_list"))
    if request.method == "POST":
        if form.is_valid():
            saved = form.save()
            record_audit(
                action=f"scheme_{'changed' if instance else 'added'}",
                description=f"Scheme '{saved.name}' {'updated' if instance else 'added'}: {saved.offer_label}.",
                actor=request.user,
                target=saved,
            )
            messages.success(request, f"Scheme '{saved.name}' {'updated' if instance else 'added'}.")
            return redirect(with_touched_row(back["back_url"], saved.pk))
        messages.error(request, "Please fix the scheme details below.")
    return render(request, "pricing/scheme_form.html", _context(request, "schemes", form=form, instance=instance, **back))


@pricing_access("schemes", ACCESS_MANAGE)
def scheme_add(request):
    return _scheme_form(request)


@pricing_access("schemes", ACCESS_MANAGE)
def scheme_edit(request, pk):
    return _scheme_form(request, get_object_or_404(Scheme, pk=pk))


@pricing_access("schemes", ACCESS_MANAGE)
@require_POST
def scheme_toggle(request, pk):
    scheme = get_object_or_404(Scheme, pk=pk)
    scheme.is_active = not scheme.is_active
    scheme.save(update_fields=["is_active"])
    record_audit(
        action="scheme_switched",
        description=f"Scheme '{scheme.name}' switched {'on' if scheme.is_active else 'off'}.",
        actor=request.user,
        target=scheme,
    )
    messages.success(request, f"Scheme '{scheme.name}' switched {'on' if scheme.is_active else 'off'}.")
    return redirect(safe_next_url(request) or reverse("pricing_scheme_list"))
