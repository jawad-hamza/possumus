"""Stock returns: market returns from customers and returns to suppliers."""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from pos.models import Product
from pos.services.access import ACCESS_MANAGE
from pos.services.navigation import return_context

from ..forms import StockReturnHeaderForm
from ..models import StockReturn, StockReturnItem
from ..services.common import DistributionError, parse_pack_lines, product_choice_data
from ..services.stock_returns import create_stock_return, damaged_held
from .common import can_manage, date_param, distribution_access, pk_param, tabs_context

KIND_FILTERS = (("", "All"), (StockReturn.FROM_CUSTOMER, "Market returns"), (StockReturn.TO_SUPPLIER, "To suppliers"))


@distribution_access()
def return_list(request):
    kind = request.GET.get("kind") or ""
    if kind not in dict(KIND_FILTERS):
        kind = ""
    start = date_param(request, "start")
    end = date_param(request, "end")
    returns = StockReturn.objects.select_related("customer", "supplier", "load_form").annotate(line_count=Count("items"))
    if kind:
        returns = returns.filter(kind=kind)
    if start:
        returns = returns.filter(return_date__gte=start)
    if end:
        returns = returns.filter(return_date__lte=end)
    page = Paginator(returns.order_by("-return_date", "-id"), 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "distribution/return_list.html",
        {
            "returns": list(page.object_list),
            "page_obj": page,
            "page_querystring": query.urlencode(),
            "kind": kind,
            "kind_filters": KIND_FILTERS,
            "start": start,
            "end": end,
            "can_manage": can_manage(request.user),
            **tabs_context("returns"),
        },
    )


def _catalogue(kind):
    products = list(Product.objects.filter(is_active=True).order_by("name", "variant_label"))
    held = damaged_held() if kind == StockReturn.TO_SUPPLIER else {}
    data = {}
    for product in products:
        entry = product_choice_data(product)
        if kind == StockReturn.TO_SUPPLIER:
            # A supplier is charged at cost; damaged goods held for a claim can be sent back too.
            for level in entry["levels"]:
                level["price"] = level["cost"]
            qty, _ = held.get(product.pk, (0, 0))
            entry["damaged_text"] = product.format_quantity(qty) if qty else ""
        data[str(product.pk)] = entry
    return products, data


@distribution_access(ACCESS_MANAGE)
def return_add(request, kind):
    if kind not in (StockReturn.FROM_CUSTOMER, StockReturn.TO_SUPPLIER):
        kind = StockReturn.FROM_CUSTOMER
    back = return_context(request, reverse("distribution_return_list"))
    initial = {}
    load_id = pk_param(request, "load")
    if load_id and kind == StockReturn.FROM_CUSTOMER:
        initial["load_form"] = load_id
    customer_id = pk_param(request, "customer")
    if customer_id:
        initial["customer"] = customer_id
    supplier_id = pk_param(request, "supplier")
    if supplier_id:
        initial["supplier"] = supplier_id
    form = StockReturnHeaderForm(request.POST or None, kind=kind, initial=initial)
    products, product_data = _catalogue(kind)
    typed = [{"product_id": "", "quantity": "", "unit": "", "rate": "", "condition": "saleable", "error": ""}]
    if request.method == "POST":
        lines, problems, typed = parse_pack_lines(request.POST, rate_required=False, with_condition=True)
        if form.is_valid() and not problems:
            data = form.cleaned_data
            try:
                saved = create_stock_return(
                    kind=kind,
                    lines=lines,
                    reason=data["reason"],
                    settlement=data["settlement"],
                    customer=data.get("customer"),
                    supplier=data.get("supplier"),
                    bank_account=data.get("bank_account"),
                    return_date=data["return_date"],
                    notes=data.get("notes", ""),
                    load_form=data.get("load_form"),
                    user=request.user,
                )
            except DistributionError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"{saved.get_kind_display()} {saved.display_number} saved, {saved.total_amount:,.2f}.")
                return redirect("distribution_return_detail", pk=saved.pk)
        else:
            for problem in problems:
                messages.error(request, problem)
            if not form.is_valid():
                messages.error(request, "Please fix the return details below.")
        if not typed:
            typed = [{"product_id": "", "quantity": "", "unit": "", "rate": "", "condition": "saleable", "error": ""}]
    return render(
        request,
        "distribution/return_form.html",
        {
            "form": form,
            "kind": kind,
            "is_customer": kind == StockReturn.FROM_CUSTOMER,
            "lines": typed,
            "products": products,
            "product_data": product_data,
            "conditions": StockReturnItem.CONDITION_CHOICES,
            **back,
            **tabs_context("returns"),
        },
    )


@distribution_access()
def return_detail(request, pk):
    stock_return = get_object_or_404(
        StockReturn.objects.select_related("customer", "supplier", "bank_account", "load_form", "voucher", "created_by"),
        pk=pk,
    )
    return render(
        request,
        "distribution/return_detail.html",
        {
            "stock_return": stock_return,
            "items": list(stock_return.items.select_related("product")),
            "voucher_lines": list(stock_return.voucher.lines.select_related("account")) if stock_return.voucher_id else [],
            **return_context(request, reverse("distribution_return_list")),
            **tabs_context("returns"),
        },
    )
