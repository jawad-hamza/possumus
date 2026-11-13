"""Purchases (list/add/detail/edit/return) and supplier creation."""
import json
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from accounts.services.audit import record_audit
from accounts.services.bank_receipts import UPLOAD_FIELD as RECEIPT_FIELD, attach_optional_receipt, attach_receipt
from accounts.services.pos_posting import post_purchase_from_pos
from ..forms import (
    PurchaseForm,
    PurchaseItemFormSet,
    PurchaseReturnForm,
    QuickBrandForm,
    QuickCategoryForm,
    QuickProductForm,
    SupplierForm,
    default_stock_unit,
)
from ..models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_NAMES,
    PACK_PIECE,
    PACK_SHORT_LABELS,
    Product,
    Purchase,
    PurchaseEditLog,
    PurchaseItem,
    PurchaseReturn,
    packaging_enabled,
)
from ..services.access import (
    ACCESS_MANAGE,
    any_module_access_required,
    feature_required,
    module_access_required,
)
from ..services.navigation import get_safe_next_url, return_context, with_query, with_touched_row
from ..services.purchase_balances import (
    annotate_purchase_balances,
    outstanding_purchase_total,
    running_purchase_balances,
)
from ..services.builty import freight_by_builty, freight_by_item, freight_by_purchase, landed_costs
from ..services.modules import module_enabled
from ..services.returns import create_purchase_return, return_row
from ._shared import _party_opening_form, _save_party_opening


def _packaging_config():
    """What purchase.js needs to know about cartons and boxes, as JSON for the page's config block."""
    enabled = packaging_enabled()
    return json.dumps(
        {
            "enabled": enabled,
            # The unit a line starts in; a product without it starts in the next smaller one it has.
            "defaultUnit": default_stock_unit() if enabled else PACK_PIECE,
            "levels": [
                {"code": code, "name": PACK_NAMES[code], "label": PACK_SHORT_LABELS[code]}
                for code in (PACK_CARTON, PACK_BOX)
            ],
        }
    )


def _purchase_setup_context(next_url):
    return {
        "purchase_packaging": _packaging_config(),
        "supplier_add_url": f"{reverse('supplier_add')}?{urlencode({'next': next_url})}",
        "quick_brand_form": QuickBrandForm(prefix="quick_brand"),
        "quick_category_form": QuickCategoryForm(prefix="quick_category"),
        "quick_supplier_form": SupplierForm(prefix="quick_supplier"),
        "quick_product_form": QuickProductForm(prefix="quick_product"),
        "quick_create_brand_url": reverse("quick_create_brand"),
        "quick_create_category_url": reverse("quick_create_category"),
        "quick_create_supplier_url": reverse("quick_create_supplier"),
        "quick_create_product_url": reverse("quick_create_product"),
    }


@any_module_access_required(("purchases", "parties"), minimum=ACCESS_MANAGE)
def add_supplier_from_pos(request):
    next_url = get_safe_next_url(request, "supplier_list")
    return_to_purchase = reverse("purchase_list") in next_url or reverse("purchase_add") in next_url or "/purchases/" in next_url

    form = SupplierForm(request.POST or None)
    opening_form = _party_opening_form(request, "supplier")
    if request.method == "POST":
        supplier = None
        if form.is_valid() and (opening_form is None or opening_form.is_valid()):
            try:
                # The supplier and the amount already owed to them are saved together or not at all.
                with transaction.atomic():
                    supplier = form.save()
                    _save_party_opening(request, supplier, "supplier", opening_form)
            except ValueError as exc:
                supplier = None
                opening_form.add_error(None, str(exc))

        if supplier is not None:
            messages.success(request, f"Supplier '{supplier.name}' added.")

            if return_to_purchase and (reverse("purchase_add") in next_url or "/edit/" in next_url):
                # Back on the purchase with the new supplier chosen; the typed lines come back from the draft.
                return redirect(with_query(next_url, supplier_id=supplier.id))
            if request.POST.get("next") or request.GET.get("next"):
                return redirect(with_touched_row(next_url, supplier.id))
            return redirect("supplier_detail", id=supplier.id)

        messages.error(request, "Please fix the supplier details below.")

    return render(
        request,
        "pos/add_supplier.html",
        {
            "form": form,
            "opening_form": opening_form,
            "next_url": next_url,
            "return_to_purchase": return_to_purchase,
        },
    )


def _active_purchase_items(formset):
    """(form, unsaved item) pairs for the lines being kept, plus their total."""
    lines = []
    total = Decimal("0")
    for form in formset.forms:
        if not getattr(form, "cleaned_data", None):
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        if not form.cleaned_data.get("product"):
            continue

        item = form.save(commit=False)
        lines.append((form, item))
        total += item.line_total

    return lines, total


class _PurchaseLineError(Exception):
    """Carries a stock/return rule failure out of the savepoint together with the line it belongs to."""

    def __init__(self, form, item, reasons):
        super().__init__("; ".join(reasons))
        self.form = form
        self.item = item
        self.reasons = reasons


_STOCK_ALREADY_USED = (
    "Some of this stock has already been sold or adjusted out, so the purchase cannot take it back."
)
_FRIENDLY_LINE_ERRORS = {
    "Cannot reduce stock below zero": _STOCK_ALREADY_USED,
    "Cannot change this purchase item because existing stock has already been consumed": _STOCK_ALREADY_USED,
}


def _line_error_reasons(exc):
    if isinstance(exc, ProtectedError):
        return ["Other records (such as supplier returns or a builty) still point at this line, so it cannot be removed."]
    reasons = exc.messages if isinstance(exc, ValidationError) else [str(exc)]
    return [_FRIENDLY_LINE_ERRORS.get(reason, reason) for reason in reasons]


def _remove_purchase_lines(formset):
    for form in formset.deleted_forms:
        item = form.instance
        if not item.pk:
            continue
        try:
            item.delete()
        except (ValidationError, ProtectedError) as exc:
            raise _PurchaseLineError(form, item, _line_error_reasons(exc)) from exc


def _packing_words(product):
    return product.packing_display or "no cartons or boxes"


def _apply_line_packing(form, product):
    """Write the packing typed on a purchase line to its product.

    ``product`` is the row just re-read from the database: the form only changed its own copy
    to validate the line. Returns (product, packing before, packing after), or None when the
    line left the packing alone.
    """
    change = getattr(form, "packing_change", None)
    if not change:
        return None
    pieces_per_box, boxes_per_carton = change
    if (product.pieces_per_box, product.boxes_per_carton) == (pieces_per_box, boxes_per_carton):
        return None
    before = _packing_words(product)
    product.pieces_per_box, product.boxes_per_carton = pieces_per_box, boxes_per_carton
    product.save(update_fields=["pieces_per_box", "boxes_per_carton"])
    return product, before, _packing_words(product)


def _save_purchase_lines(purchase, lines):
    """Save the kept lines. Returns the packing changes the lines made to their products."""
    repacked = []
    for form, item in lines:
        item.purchase = purchase
        # The same product may sit on two lines: reload it so the second line averages its cost
        # against what the first line just wrote, not against the value loaded with the form.
        item.product = Product.objects.get(pk=item.product_id)
        # The line's unit is sized from the product, so packing typed on the line is written first.
        change = _apply_line_packing(form, item.product)
        if change:
            repacked.append(change)
        try:
            item.save()
        except ValidationError as exc:
            raise _PurchaseLineError(form, item, _line_error_reasons(exc)) from exc
    return repacked


def _record_packing_changes(request, purchase, repacked):
    """A purchase that changed a product's packing leaves that on the audit trail."""
    for product, before, after in repacked:
        record_audit(
            action="product_packing_updated",
            description=(
                f"Packing of {product.display_name} changed from {before} to {after} on purchase #{purchase.id}."
            ),
            actor=request.user,
            target=product,
            metadata={"purchase_id": purchase.id, "before": before, "after": after},
        )


def _show_purchase_line_error(purchase_form, exc):
    """Put the failure next to the offending line and summarise it above the form."""
    product_name = exc.item.product.display_name if exc.item.product_id else "This line"
    for reason in exc.reasons:
        purchase_form.add_error(None, f"{product_name}: {reason}")
    if "quantity" in exc.form.fields:
        exc.form.add_error("quantity", " ".join(exc.reasons))


PURCHASES_PER_PAGE = 25


@module_access_required("purchases")
def purchase_list(request):
    purchases_queryset = Purchase.objects.select_related("supplier").order_by("-created_at", "-id")
    month_start = timezone.localdate().replace(day=1)

    this_month_total = purchases_queryset.filter(created_at__date__gte=month_start).aggregate(
        total=Coalesce(Sum("total_amount"), Decimal("0"))
    )["total"]
    # Only the visible page gets per-bill balances; the headline figure comes from totals.
    page_obj = Paginator(purchases_queryset, PURCHASES_PER_PAGE).get_page(request.GET.get("page"))
    purchases = annotate_purchase_balances(page_obj.object_list)
    running = running_purchase_balances()
    freight = freight_by_purchase([purchase.id for purchase in purchases]) if module_enabled("builty") else {}
    for purchase in purchases:
        purchase.running_balance = running.get(purchase.id, purchase.report_balance_due)
        purchase.freight_total = freight.get(purchase.id)
    outstanding_total = outstanding_purchase_total()

    return render(
        request,
        "pos/purchase_list.html",
        {
            "purchases": purchases,
            "page_obj": page_obj,
            "purchase_count": page_obj.paginator.count,
            "this_month_total": this_month_total,
            "outstanding_total": outstanding_total,
        },
    )


@module_access_required("purchases", minimum=ACCESS_MANAGE)
@transaction.atomic
def purchase_add(request):
    initial = {}
    returned_supplier = (request.GET.get("supplier_id") or "").strip()
    if request.method != "POST" and returned_supplier.isdigit():
        initial["supplier"] = int(returned_supplier)
    purchase_form = PurchaseForm(request.POST or None, request.FILES or None, initial=initial)
    back = return_context(request, reverse("purchase_list"))
    formset = PurchaseItemFormSet(
        request.POST or None,
        queryset=PurchaseItem.objects.none(),
        prefix="items",
    )

    if request.method == "POST":
        if purchase_form.is_valid() and formset.is_valid():
            lines, calculated_total = _active_purchase_items(formset)
            supplier = purchase_form.cleaned_data["supplier"]
            paid_amount = purchase_form.cleaned_data["paid_amount"]

            if paid_amount > calculated_total:
                purchase_form.add_error("paid_amount", "Paid amount cannot exceed purchase total.")
            if calculated_total > paid_amount and supplier is None:
                purchase_form.add_error("supplier", "Supplier is required when any balance remains unpaid.")
            if not purchase_form.errors:
                try:
                    # Savepoint: a rule broken by any line undoes the whole purchase, and the
                    # user gets the form back with the reason instead of an error page.
                    with transaction.atomic():
                        purchase = purchase_form.save()
                        repacked = _save_purchase_lines(purchase, lines)
                        purchase.refresh_from_db()
                        post_purchase_from_pos(
                            purchase_id=purchase.id,
                            supplier=purchase.supplier,
                            total_amount=purchase.total_amount,
                            paid_amount=purchase.paid_amount,
                            payment_method=purchase.payment_method,
                            bank_account=purchase.bank_account,
                            purchase_date=timezone.localtime(purchase.created_at).date(),
                        )
                        attach_receipt(purchase, purchase_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user, audit=False)
                except _PurchaseLineError as exc:
                    _show_purchase_line_error(purchase_form, exc)
                except (ValidationError, ValueError) as exc:
                    for reason in _line_error_reasons(exc):
                        purchase_form.add_error(None, reason)
            if not purchase_form.errors:
                messages.success(request, f"Purchase #{purchase.id} saved successfully.")
                record_audit(
                    action="purchase_created",
                    description=f"Purchase #{purchase.id} recorded.",
                    actor=request.user,
                    target=purchase,
                    metadata={"total_amount": str(purchase.total_amount), "payment_method": purchase.payment_method},
                )
                _record_packing_changes(request, purchase, repacked)
                if back["next_url"]:
                    return redirect(with_touched_row(back["next_url"], purchase.id))
                return redirect("purchase_detail", pk=purchase.id)
        messages.error(request, "Please review the purchase form and line items.")

    context = {
        "purchase_form": purchase_form,
        "formset": formset,
        **back,
    }
    context.update(_purchase_setup_context(with_query(reverse("purchase_add"), next=back["next_url"] or None)))
    return render(request, "pos/purchase_add.html", context)


@module_access_required("purchases")
def purchase_detail(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier").prefetch_related(
            "items__product", "edit_logs", "returns__items__product"
        ),
        pk=pk,
    )
    purchase = annotate_purchase_balances([purchase])[0]
    items = list(purchase.items.all())
    # Freight from builties still in force: each line's share and what one unit landed at.
    freight = freight_by_item([item.id for item in items])
    for item in items:
        item.freight = freight.get(item.id)
        if item.freight:
            item.landed_unit_cost, item.landed_piece_cost = landed_costs(item, item.freight)
    builties = []
    if module_enabled("builty"):
        builties = list(purchase.builties.select_related("transporter").order_by("-date", "-id"))
        shares = freight_by_builty(purchase.id)
        for builty in builties:
            builty.share_here = shares.get(builty.id, Decimal("0"))
    freight_total = sum(freight.values(), Decimal("0"))
    return render(
        request,
        "pos/purchase_detail.html",
        {
            "purchase": purchase,
            "items": items,
            "freight_total": freight_total,
            "landed_total": Decimal(purchase.total_amount or 0) + freight_total,
            "has_freight": bool(freight),
            "builties": builties,
        },
    )


@module_access_required("purchases", minimum=ACCESS_MANAGE)
@transaction.atomic
def purchase_edit(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related("supplier"), pk=pk)
    initial = {}
    returned_supplier = (request.GET.get("supplier_id") or "").strip()
    if request.method != "POST" and returned_supplier.isdigit():
        initial["supplier"] = int(returned_supplier)
    purchase_form = PurchaseForm(request.POST or None, request.FILES or None, instance=purchase, initial=initial)
    back = return_context(request, reverse("purchase_detail", args=[purchase.id]))
    formset = PurchaseItemFormSet(
        request.POST or None,
        queryset=purchase.items.select_related("product").all(),
        prefix="items",
    )

    if request.method == "POST":
        reason = request.POST.get("reason", "").strip()

        if purchase_form.is_valid() and formset.is_valid():
            lines, calculated_total = _active_purchase_items(formset)
            supplier = purchase_form.cleaned_data["supplier"]
            paid_amount = purchase_form.cleaned_data["paid_amount"]

            if not reason:
                messages.error(request, "Reason for edit is required.")
            else:
                if paid_amount > calculated_total:
                    purchase_form.add_error("paid_amount", "Paid amount cannot exceed purchase total.")
                if calculated_total > paid_amount and supplier is None:
                    purchase_form.add_error("supplier", "Supplier is required when any balance remains unpaid.")

            if reason and not purchase_form.errors:
                old_total = Purchase.objects.values_list("total_amount", flat=True).get(pk=purchase.pk)
                try:
                    with transaction.atomic():
                        purchase = purchase_form.save(commit=False)
                        purchase.last_edited_by = request.user if getattr(request, "user", None) and request.user.is_authenticated else None
                        purchase.last_edited_at = timezone.now()
                        purchase.edit_reason = reason
                        purchase.save()

                        _remove_purchase_lines(formset)
                        repacked = _save_purchase_lines(purchase, lines)

                        purchase.refresh_from_db()
                        post_purchase_from_pos(
                            purchase_id=purchase.id,
                            supplier=purchase.supplier,
                            total_amount=purchase.total_amount,
                            paid_amount=purchase.paid_amount,
                            payment_method=purchase.payment_method,
                            bank_account=purchase.bank_account,
                            purchase_date=timezone.localtime(purchase.created_at).date(),
                        )
                        attach_receipt(purchase, purchase_form.cleaned_data.get(RECEIPT_FIELD), actor=request.user)
                except _PurchaseLineError as exc:
                    _show_purchase_line_error(purchase_form, exc)
                except (ValidationError, ValueError) as exc:
                    for reason_text in _line_error_reasons(exc):
                        purchase_form.add_error(None, reason_text)
                if purchase_form.errors:
                    # The savepoint undid the database; bring the in-memory purchase back in line.
                    purchase.refresh_from_db()

            if reason and not purchase_form.errors:
                record_audit(
                    action="purchase_updated",
                    description=f"Purchase #{purchase.id} updated. Reason: {reason}",
                    actor=request.user,
                    target=purchase,
                    metadata={"old_total": str(old_total), "new_total": str(purchase.total_amount)},
                )
                _record_packing_changes(request, purchase, repacked)
                PurchaseEditLog.objects.create(
                    purchase=purchase,
                    edited_by=purchase.last_edited_by,
                    reason=reason,
                    old_total=old_total,
                    new_total=purchase.total_amount,
                )

                messages.success(request, f"Purchase #{purchase.id} updated.")
                if back["next_url"]:
                    return redirect(with_touched_row(back["next_url"], purchase.id))
                return redirect("purchase_detail", pk=purchase.id)

        messages.error(request, "Please review the purchase changes.")

    context = {
        "purchase": purchase,
        "purchase_form": purchase_form,
        "formset": formset,
        **back,
    }
    context.update(_purchase_setup_context(with_query(reverse("purchase_edit", args=[purchase.id]), next=back["next_url"] or None)))
    return render(request, "pos/purchase_edit.html", context)


@module_access_required("purchases", minimum=ACCESS_MANAGE)
@feature_required("returns")
def purchase_return_view(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("supplier").prefetch_related("items__product", "items__return_items", "returns__items__product"),
        pk=pk,
    )
    purchase = annotate_purchase_balances([purchase])[0]
    initial = {"settlement_method": PurchaseReturn.PAYABLE if purchase.supplier else PurchaseReturn.CASH}
    form = PurchaseReturnForm(request.POST or None, request.FILES or None, initial=initial)

    item_rows = [return_row(item) for item in purchase.items.all()]

    if request.method == "POST" and form.is_valid():
        quantities = {row["item"].id: request.POST.get(f"qty_{row['item'].id}", "0") for row in item_rows}
        units = {row["item"].id: request.POST.get(f"unit_{row['item'].id}", "") for row in item_rows}
        try:
            purchase_return = create_purchase_return(
                purchase=purchase,
                quantities=quantities,
                units=units,
                settlement_method=form.cleaned_data["settlement_method"],
                bank_account=form.cleaned_data.get("bank_account"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
            attach_optional_receipt(request, purchase_return, form.cleaned_data.get(RECEIPT_FIELD))
            messages.success(request, f"Purchase return #{purchase_return.id} recorded.")
            return redirect("purchase_detail", pk=purchase.id)
        except (ValueError, ValidationError) as exc:
            form.add_error(None, " ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
            messages.error(request, "Please review the purchase return details.")

    existing_returns = purchase.returns.prefetch_related("items__product").order_by("-created_at", "-id")
    return render(
        request,
        "pos/purchase_return.html",
        {
            "purchase": purchase,
            "form": form,
            "item_rows": item_rows,
            "existing_returns": existing_returns,
        },
    )
