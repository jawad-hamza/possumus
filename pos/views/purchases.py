"""Purchases (list/add/detail/edit/return) and supplier creation."""
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
from accounts.services.pos_posting import post_purchase_from_pos
from ..forms import (
    PurchaseForm,
    PurchaseItemFormSet,
    PurchaseReturnForm,
    QuickBrandForm,
    QuickCategoryForm,
    QuickProductForm,
    SupplierForm,
)
from ..models import (
    Product,
    Purchase,
    PurchaseEditLog,
    PurchaseItem,
    PurchaseReturn,
)
from ..services.access import (
    ACCESS_MANAGE,
    any_module_access_required,
    feature_required,
    module_access_required,
)
from ..services.navigation import get_safe_next_url, return_context, with_query, with_touched_row
from ..services.purchase_balances import annotate_purchase_balances, outstanding_purchase_total
from ..services.returns import create_purchase_return
from ._shared import _party_opening_form, _save_party_opening


def _purchase_setup_context(next_url):
    return {
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
        return ["Other records (such as supplier returns) still point at this line, so it cannot be removed."]
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


def _save_purchase_lines(purchase, lines):
    for form, item in lines:
        item.purchase = purchase
        # The same product may sit on two lines: reload it so the second line averages its cost
        # against what the first line just wrote, not against the value loaded with the form.
        item.product = Product.objects.get(pk=item.product_id)
        try:
            item.save()
        except ValidationError as exc:
            raise _PurchaseLineError(form, item, _line_error_reasons(exc)) from exc


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
    purchase_form = PurchaseForm(request.POST or None, initial=initial)
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
                        _save_purchase_lines(purchase, lines)
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
        Purchase.objects.select_related("supplier").prefetch_related("items__product", "edit_logs"),
        pk=pk,
    )
    purchase = annotate_purchase_balances([purchase])[0]
    return render(request, "pos/purchase_detail.html", {"purchase": purchase})


@module_access_required("purchases", minimum=ACCESS_MANAGE)
@transaction.atomic
def purchase_edit(request, pk):
    purchase = get_object_or_404(Purchase.objects.select_related("supplier"), pk=pk)
    initial = {}
    returned_supplier = (request.GET.get("supplier_id") or "").strip()
    if request.method != "POST" and returned_supplier.isdigit():
        initial["supplier"] = int(returned_supplier)
    purchase_form = PurchaseForm(request.POST or None, instance=purchase, initial=initial)
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
                        _save_purchase_lines(purchase, lines)

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
    form = PurchaseReturnForm(request.POST or None, initial=initial)

    item_rows = []
    for item in purchase.items.all():
        returned_qty = sum(return_item.quantity for return_item in item.return_items.all())
        item_rows.append(
            {
                "item": item,
                "returned_qty": returned_qty,
                "returnable_qty": max(item.quantity - returned_qty, 0),
            }
        )

    if request.method == "POST" and form.is_valid():
        quantities = {row["item"].id: request.POST.get(f"qty_{row['item'].id}", "0") for row in item_rows}
        try:
            purchase_return = create_purchase_return(
                purchase=purchase,
                quantities=quantities,
                settlement_method=form.cleaned_data["settlement_method"],
                bank_account=form.cleaned_data.get("bank_account"),
                reason=form.cleaned_data["reason"],
                notes=form.cleaned_data.get("notes") or "",
                actor=request.user,
            )
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
