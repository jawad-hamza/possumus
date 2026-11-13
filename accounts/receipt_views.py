"""
The receipt page of a bank payment, and the file itself.

Both are reached with the payment's kind and id (/accounts/receipts/sale/12/), never with a
storage path. Each asks for the access the payment itself needs, so a cashier sees the proof
of his own sales only and a purchases role cannot open a customer's bank slip.
"""

import posixpath
from functools import wraps
from pathlib import Path

from django.contrib import messages
from django.core.exceptions import PermissionDenied, SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils._os import safe_join
from django.utils.text import slugify
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_http_methods
from django.views.static import serve as serve_media_file

from accounts.models import BankReceipt
from accounts.receipt_forms import BankReceiptUploadForm
from accounts.services.bank_receipts import (
    UPLOAD_FIELD,
    ReceiptStorageError,
    attach_receipt,
    can_attach_receipt,
    can_change_receipt,
    can_view_receipt,
    describe,
    get_receipt,
    kind_for_slug,
    record_queryset,
    remove_receipt,
)
from pos.services.access import default_home_url, workspace_gate_response
from pos.services.navigation import return_context, with_query


def _workspace_open(view_func):
    """The front gate every protected page uses: signed in, business set up, licence in force."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        blocked = workspace_gate_response(request)
        if blocked is not None:
            return blocked
        return view_func(request, *args, **kwargs)

    return wrapped


def _payment(kind_slug, pk):
    kind = kind_for_slug(kind_slug)
    if kind is None:
        raise Http404("Unknown kind of payment.")
    return get_object_or_404(record_queryset(kind), pk=pk)


def _file_in_storage(receipt) -> bool:
    """False when the row survived (a restored backup, a copied database) but its file did not."""
    try:
        return bool(receipt.file) and receipt.file.storage.exists(receipt.file.name)
    except Exception:  # an unreachable remote storage must not take the page down
        return False


@_workspace_open
@require_http_methods(["GET", "POST"])
def bank_receipt(request, kind, pk):
    record = _payment(kind, pk)
    user = request.user
    if not can_view_receipt(user, record):
        messages.error(request, "Your account does not have access to this payment or its receipt.")
        return redirect(default_home_url(user))

    facts = describe(record)
    back = return_context(request, facts["home"])
    here = with_query(facts["url"], next=back["next_url"] or None)
    receipt = get_receipt(record)
    may_change = can_change_receipt(user, record)
    # Only a bank payment has something to prove; anyone who can open it may add a missing proof.
    may_attach = receipt is None and facts["is_bank"] and can_attach_receipt(user, record)
    form = BankReceiptUploadForm()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "remove":
            if receipt is None:
                messages.info(request, f"{facts['label']} has no bank receipt to remove.")
            elif not may_change:
                messages.error(request, "Removing a receipt needs manage access. Ask an admin.")
            else:
                remove_receipt(record, actor=user)
                messages.success(request, f"Bank receipt removed from {facts['label']}.")
            return redirect(here)

        if receipt is not None and not may_change:
            messages.error(request, "Replacing a receipt needs manage access. Ask an admin.")
            return redirect(here)
        if receipt is None and not may_attach:
            messages.error(request, f"A bank receipt cannot be added to {facts['label']}.")
            return redirect(here)

        form = BankReceiptUploadForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                attach_receipt(record, form.cleaned_data[UPLOAD_FIELD], actor=user)
            except ReceiptStorageError as exc:
                form.add_error(UPLOAD_FIELD, str(exc))
            else:
                verb = "replaced on" if receipt is not None else "attached to"
                messages.success(request, f"Bank receipt {verb} {facts['label']}.")
                # Back to where the user was working: the invoice, the statement, the expense list.
                return redirect(back["back_url"])

    return render(
        request,
        "accounts/bank_receipt.html",
        {
            **back,
            "payment": record,
            "facts": facts,
            "receipt": receipt,
            "file_missing": receipt is not None and not _file_in_storage(receipt),
            # v= changes with every replacement, so a frame or image never shows the previous file.
            "file_url": with_query(reverse("bank_receipt_file", args=[kind, record.pk]), v=receipt.pk) if receipt is not None else "",
            "may_change": may_change,
            "may_attach": may_attach,
            "form": form,
            "form_action": here,
            "confirm_remove": request.GET.get("confirm") == "remove" and receipt is not None and may_change,
            "confirm_remove_url": with_query(here, confirm="remove"),
            "cancel_url": here,
        },
    )


@_workspace_open
@require_GET
@xframe_options_sameorigin  # the receipt page shows a PDF in a frame of its own origin
def bank_receipt_file(request, kind, pk):
    record = _payment(kind, pk)
    if not can_view_receipt(request.user, record):
        raise PermissionDenied("Your account does not have access to this receipt.")
    receipt = get_receipt(record)
    if receipt is None or not receipt.file:
        raise Http404("This payment has no bank receipt.")
    try:
        handle = receipt.file.open("rb")
    except Exception as exc:  # the row is there but the file is not (see _file_in_storage)
        raise Http404("The receipt file is missing from storage.") from exc

    extension = "." + receipt.file.name.rsplit(".", 1)[-1] if "." in receipt.file.name else ""
    filename = f"bank-receipt-{slugify(describe(record)['label'])}{extension}"
    response = FileResponse(
        handle,
        as_attachment=request.GET.get("download") == "1",
        filename=filename,
        # The type that was detected from the content when the file was accepted.
        content_type=receipt.mime_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def serve_public_media(request, path, document_root=None):
    """Django's own media view (used when no web server sits in front), minus the receipts folder.

    /media/ is open without a sign-in so the logo can show on the sign-in page. Bank receipts
    live in the same storage and must not be reachable that way: the only door to them is
    bank_receipt_file above. The check is made on the resolved path, so "..", a backslash
    or another letter case cannot walk around it.
    """
    try:
        target = Path(safe_join(document_root, posixpath.normpath(path).lstrip("/"))).resolve()
    except (SuspiciousFileOperation, ValueError, OSError) as exc:
        raise Http404("Not found.") from exc
    private_root = (Path(document_root) / BankReceipt.UPLOAD_FOLDER).resolve()
    if target == private_root or private_root in target.parents:
        raise Http404("Not found.")
    return serve_media_file(request, path, document_root=document_root)
