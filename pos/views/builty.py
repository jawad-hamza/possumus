"""Builty (freight bill) screens: the list, the day screen that records one, its page, its reversal."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from accounts.services.bank_receipts import UPLOAD_FIELD as RECEIPT_FIELD
from accounts.services.bank_receipts import attach_receipt

from ..forms import SupplierForm
from ..forms_builty import BuiltyForm, BuiltyReverseForm
from ..models import Builty, Purchase, Supplier
from ..services.access import ACCESS_MANAGE, feature_required, module_access_required
from ..services.builty import (
    BASIS_HINTS,
    counted_text,
    freight_by_purchase,
    post_builty,
    preview_line,
    reverse_builty,
)
from ..services.navigation import return_context, with_touched_row

BUILTIES_PER_PAGE = 25


def _int(value):
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


def _purchase_day(purchase):
    return timezone.localtime(purchase.created_at).date()


@module_access_required("purchases")
@feature_required("builty")
def builty_list(request):
    queryset = (
        Builty.objects.select_related("transporter", "bank_account")
        .prefetch_related("purchases")
        .order_by("-date", "-id")
    )
    page_obj = Paginator(queryset, BUILTIES_PER_PAGE).get_page(request.GET.get("page"))
    return render(
        request,
        "pos/builty_list.html",
        {
            "builties": page_obj.object_list,
            "page_obj": page_obj,
            "builty_count": page_obj.paginator.count,
        },
    )


def _day_purchases(day, supplier_id, extra_id=None):
    """The purchases recorded on that day (optionally one supplier's), plus the one the user came from."""
    purchases = Purchase.objects.filter(created_at__date=day)
    if supplier_id:
        purchases = purchases.filter(supplier_id=supplier_id)
    rows = list(
        purchases.select_related("supplier").prefetch_related("items__product").order_by("created_at", "id")
    )
    if extra_id and extra_id not in {purchase.pk for purchase in rows}:
        extra = (
            Purchase.objects.filter(pk=extra_id).select_related("supplier").prefetch_related("items__product").first()
        )
        if extra is not None:
            rows.append(extra)
            # The same order the freight is split in, so the screen and the save agree on ties.
            rows.sort(key=lambda purchase: (purchase.created_at, purchase.pk))
    freight = freight_by_purchase([purchase.pk for purchase in rows])
    for purchase in rows:
        purchase.freight_total = freight.get(purchase.pk)
        purchase.line_count = len(purchase.items.all())
    return [purchase for purchase in rows if purchase.line_count]


@module_access_required("purchases", minimum=ACCESS_MANAGE)
@feature_required("builty")
def builty_add(request):
    source = request.POST if request.method == "POST" else request.GET
    from_purchase_id = _int(source.get("purchase"))
    from_purchase = Purchase.objects.filter(pk=from_purchase_id).first() if from_purchase_id else None

    try:
        day = parse_date(str(source.get("day") or "").strip())
    except ValueError:  # well formed but not a real date, such as 2026-02-30
        day = None
    if day is None or day > timezone.localdate():
        day = _purchase_day(from_purchase) if from_purchase else timezone.localdate()
    supplier_id = _int(source.get("supplier"))
    purchases = _day_purchases(day, supplier_id, from_purchase.pk if from_purchase else None)
    shown_ids = {purchase.pk for purchase in purchases}

    default_back = reverse("purchase_detail", args=[from_purchase.pk]) if from_purchase else reverse("builty_list")
    back = return_context(request, default_back)
    form = BuiltyForm(request.POST or None, request.FILES or None, initial={"date": day})

    if request.method == "POST":
        ticked = {_int(value) for value in request.POST.getlist("purchases")} & shown_ids
        if form.is_valid():
            chosen = [purchase for purchase in purchases if purchase.pk in ticked]
            if not chosen:
                form.add_error(None, "Tick at least one purchase this freight was paid on.")
            else:
                data = form.cleaned_data
                try:
                    with transaction.atomic():
                        builty = post_builty(
                            purchases=chosen,
                            amount=data["amount"],
                            basis=data["basis"],
                            payment_method=data["payment_method"],
                            date=data["date"],
                            builty_number=data.get("builty_number") or "",
                            transporter=data.get("transporter"),
                            bank_account=data.get("bank_account"),
                            notes=data.get("notes") or "",
                            actor=request.user,
                        )
                        attach_receipt(builty, data.get(RECEIPT_FIELD), actor=request.user, audit=False)
                except (ValidationError, ValueError) as exc:
                    form.add_error(None, " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
                else:
                    messages.success(
                        request,
                        f"{builty.display_number} saved: {builty.amount} shared over {len(chosen)} purchase"
                        f"{'s' if len(chosen) != 1 else ''}.",
                    )
                    if back["next_url"]:
                        return redirect(with_touched_row(back["next_url"], builty.pk))
                    return redirect("builty_detail", pk=builty.pk)
        messages.error(request, "Please review the builty.")
    else:
        # From a purchase only that purchase starts ticked; from the day screen every one does.
        ticked = {from_purchase.pk} if from_purchase else shown_ids

    for purchase in purchases:
        purchase.is_ticked = purchase.pk in ticked

    preview = {
        "purchases": {
            str(purchase.pk): [preview_line(item) for item in sorted(purchase.items.all(), key=lambda item: item.pk)]
            for purchase in purchases
        },
        "hints": BASIS_HINTS,
    }
    suppliers = Supplier.objects.filter(is_active=True).order_by("name")
    return render(
        request,
        "pos/builty_add.html",
        {
            "form": form,
            "day": day,
            "day_is_today": day == timezone.localdate(),
            "selected_supplier": supplier_id,
            "suppliers": suppliers,
            "purchases": purchases,
            "from_purchase": from_purchase,
            "preview": preview,
            "quick_supplier_form": SupplierForm(prefix="quick_supplier"),
            "quick_create_supplier_url": reverse("quick_create_supplier"),
            **back,
        },
    )


@module_access_required("purchases")
@feature_required("builty")
def builty_detail(request, pk):
    builty = get_object_or_404(
        Builty.objects.select_related(
            "transporter", "bank_account", "created_by", "reversed_by", "voucher", "reversal_voucher"
        ).prefetch_related("purchases__supplier"),
        pk=pk,
    )
    allocations = list(
        builty.allocations.select_related("purchase_item__purchase", "purchase_item__product", "product").order_by("id")
    )
    for allocation in allocations:
        allocation.counted_display = counted_text(allocation.product, allocation.counted_as, allocation.units_counted)
    vouchers = [voucher for voucher in (builty.voucher, builty.reversal_voucher) if voucher is not None]
    for voucher in vouchers:
        voucher.line_rows = list(voucher.lines.select_related("account").order_by("-debit", "id"))
    return render(
        request,
        "pos/builty_detail.html",
        {
            "builty": builty,
            "allocations": allocations,
            "vouchers": vouchers,
            "reverse_form": BuiltyReverseForm(),
        },
    )


@module_access_required("purchases", minimum=ACCESS_MANAGE)
@feature_required("builty")
@require_POST
def builty_reverse(request, pk):
    builty = get_object_or_404(Builty, pk=pk)
    form = BuiltyReverseForm(request.POST)
    if not form.is_valid():
        messages.error(request, " ".join(form.errors.get("reason", ["Say why the builty is being reversed."])))
        return redirect("builty_detail", pk=builty.pk)
    try:
        builty = reverse_builty(builty, reason=form.cleaned_data["reason"], actor=request.user)
    except (ValidationError, ValueError) as exc:
        messages.error(request, " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
    else:
        messages.success(request, f"{builty.display_number} reversed.")
    return redirect("builty_detail", pk=builty.pk)
