"""
Bank receipts: the proof (a photo, a screenshot or the bank's PDF slip) behind a bank payment.

This module owns the whole rule, so every screen answers the same way:

- KINDS: which records can carry a receipt, and when each of them is a bank payment.
  While Settings > "Require a receipt for bank payments" is on, the entry points refuse
  a till sale, a purchase, a builty (freight bill), a party settlement, an expense and an
  expense payment made by bank without one (the till and the API through request_upload, the forms through
  accounts.receipt_forms.BankReceiptFormMixin). Refunds and money desk entries may
  carry a receipt but never must.
- check_upload: what a receipt file may be. JPG, PNG, WEBP or PDF, judged by the
  content of the file and never by its name, up to MAX_RECEIPT_UPLOAD_BYTES.
- attach_receipt / remove_receipt: the only writers. A replaced or removed receipt keeps
  its row and its file (removed_at is set) and the change goes to the audit log.
- can_view_receipt / can_change_receipt: the access the record itself needs.
- receipt_marks: the receipts behind a page of ledger rows, found in a few queries.
"""

import hashlib
import io
import logging
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import PurePosixPath

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

from accounts.models import BankReceipt
from accounts.services.audit import record_audit

logger = logging.getLogger(__name__)

UPLOAD_FIELD = "bank_receipt"
DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# A whole party statement can point at thousands of records: look them up this many at a time.
MARK_CHUNK = 400
BANK = "bank"

# detected content -> (stored content type, stored extension)
JPEG = ("image/jpeg", ".jpg")
PNG = ("image/png", ".png")
WEBP = ("image/webp", ".webp")
PDF = ("application/pdf", ".pdf")
PILLOW_FORMATS = {"JPEG": JPEG, "PNG": PNG, "WEBP": WEBP}
ACCEPT_ATTRIBUTE = "image/jpeg,image/png,image/webp,application/pdf"
ACCEPTED_WORDS = "a JPG, PNG or WEBP image, or a PDF"

MISSING_MESSAGE = "Attach the bank receipt (a photo, a screenshot or the PDF slip) for this bank payment."
WRONG_TYPE_MESSAGE = f"A bank receipt must be {ACCEPTED_WORDS}. This file is something else."
EMPTY_MESSAGE = "This receipt file is empty. Choose the photo, screenshot or PDF again."
UNREADABLE_MESSAGE = "This image could not be read. Take the photo or screenshot again, or attach the PDF."
STORAGE_MESSAGE = "The receipt could not be stored, so nothing was saved. Check the disk space and try again."


class ReceiptStorageError(ValueError):
    """The file could not be written. A ValueError, like every other refusal the posting
    services raise, so a payment being recorded is rolled back and its form says why."""


# ----------------------------------------------------------------------------- the rule

def max_upload_bytes() -> int:
    return int(getattr(settings, "MAX_RECEIPT_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES))


def max_upload_label() -> str:
    return f"{max_upload_bytes() / (1024 * 1024):g} MB"


def receipts_required() -> bool:
    """Is the shop's rule on? (Settings > Tax and till > Require a receipt for bank payments.)"""
    from pos.models import SystemSettings

    return bool(SystemSettings.load().require_bank_receipt)


def upload_help_text() -> str:
    return f"A photo, a screenshot or the PDF slip of the bank transfer, up to {max_upload_label()}."


# ------------------------------------------------------------------- what a file may be

@dataclass(frozen=True)
class CheckedUpload:
    """An uploaded receipt that passed check_upload, ready to store."""

    data: bytes
    mime_type: str
    extension: str
    original_name: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.data)


def _clean_name(name) -> str:
    base = PurePosixPath(str(name or "").replace("\\", "/")).name
    printable = "".join(ch for ch in base if ch.isprintable()).strip()
    return printable[-255:]


def _is_pdf(data: bytes) -> bool:
    # A PDF begins with its header; a few generators put a byte-order mark or a blank line first.
    return data[:1024].lstrip(b"\xef\xbb\xbf\x00\t\n\r ").startswith(b"%PDF-")


def _image_type(data: bytes):
    """(content type, extension) when the bytes are a JPG, PNG or WEBP that really decodes, else None."""
    from PIL import Image

    try:
        with warnings.catch_warnings():
            # An absurdly large picture is refused instead of being unpacked into memory.
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=tuple(PILLOW_FORMATS)) as image:
                found = PILLOW_FORMATS.get(image.format)
                image.verify()
    except Exception:  # Pillow raises many kinds of error for a damaged or disguised file
        return None
    return found


def looks_like_image(data: bytes) -> bool:
    head = data[:16]
    return head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")


def check_upload(uploaded) -> CheckedUpload:
    """Judge an uploaded file by its content. Raises ValidationError in the shop's words."""
    if isinstance(uploaded, CheckedUpload):
        return uploaded

    limit = max_upload_bytes()
    size = getattr(uploaded, "size", None)
    if size is not None and size > limit:
        raise ValidationError(f"This receipt is too large. Choose a file under {max_upload_label()}.")

    try:
        if hasattr(uploaded, "seek"):
            uploaded.seek(0)
        data = uploaded.read(limit + 1)
    except (OSError, ValueError) as exc:
        raise ValidationError(UNREADABLE_MESSAGE) from exc
    if len(data) > limit:
        raise ValidationError(f"This receipt is too large. Choose a file under {max_upload_label()}.")
    if not data:
        raise ValidationError(EMPTY_MESSAGE)

    if _is_pdf(data):
        found = PDF
    else:
        found = _image_type(data)
        if found is None:
            raise ValidationError(UNREADABLE_MESSAGE if looks_like_image(data) else WRONG_TYPE_MESSAGE)

    return CheckedUpload(
        data=data,
        mime_type=found[0],
        extension=found[1],
        original_name=_clean_name(getattr(uploaded, "name", "")),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def request_upload(request, *, bank_payment: bool, required: bool = True):
    """The receipt posted with a payment that is not a Django form (the till).

    Returns (checked upload or None, problem). A file sent with a cash or credit payment
    is ignored. `problem` is the sentence to show beside the payment fields.
    """
    if not bank_payment:
        return None, ""
    uploaded = request.FILES.get(UPLOAD_FIELD)
    if uploaded is None:
        return None, MISSING_MESSAGE if required and receipts_required() else ""
    try:
        return check_upload(uploaded), ""
    except ValidationError as exc:
        return None, " ".join(exc.messages)


# --------------------------------------------------------- which records carry a receipt

def _money(value) -> Decimal:
    return Decimal(value or 0)


def _bank_name(bank_account) -> str:
    return bank_account.display_name if bank_account is not None else "Bank"


def _local_date(moment):
    return timezone.localtime(moment).date() if moment else None


def _sale_facts(sale):
    return {
        "label": f"Invoice {sale.display_number}",
        "is_bank": sale.payment_method == BANK and not sale.is_credit and _money(sale.paid_amount) > 0,
        "amount": sale.paid_amount,
        "date": _local_date(sale.created_at),
        "party": sale.customer.name if sale.customer_id else "Walk-in customer",
        "bank": _bank_name(sale.bank_account),
        "direction": "Received into",
        "home": reverse("invoice", args=[sale.pk]),
    }


def _purchase_facts(purchase):
    return {
        "label": f"Purchase #{purchase.pk}",
        "is_bank": purchase.payment_method == BANK and _money(purchase.paid_amount) > 0,
        "amount": purchase.paid_amount,
        "date": _local_date(purchase.created_at),
        "party": purchase.supplier.name if purchase.supplier_id else "Cash purchase",
        "bank": _bank_name(purchase.bank_account),
        "direction": "Paid from",
        "home": reverse("purchase_detail", args=[purchase.pk]),
    }


def _settlement_facts(settlement):
    received = settlement.settlement_type == settlement.RECEIPT
    party = settlement.party
    party_kind = party.__class__.__name__.lower() if party is not None else ""
    home = reverse(f"{party_kind}_detail", args=[party.pk]) if party_kind in ("customer", "supplier") else reverse("parties_home")
    return {
        "label": f"{'Receipt' if received else 'Payment'} #{settlement.pk}",
        "is_bank": settlement.payment_method == BANK,
        "amount": settlement.amount,
        "date": settlement.date,
        "party": str(party) if party is not None else "",
        "bank": _bank_name(settlement.bank_account),
        "direction": "Received into" if received else "Paid from",
        "home": home,
    }


def _expense_paid_at_entry(expense) -> Decimal:
    """What was paid when the bill was recorded: later payments are their own records."""
    later = getattr(expense, "_receipt_paid_later", None)
    if later is None:
        later = expense.payments.aggregate(total=Sum("amount"))["total"]
    return _money(expense.paid_amount) - _money(later)


def _prepare_expenses(expenses):
    from accounts.models import ExpensePayment

    totals = dict(
        ExpensePayment.objects.filter(expense_id__in=[expense.pk for expense in expenses])
        .values_list("expense_id")
        .annotate(total=Sum("amount"))
    )
    for expense in expenses:
        expense._receipt_paid_later = totals.get(expense.pk) or Decimal("0")


def _expense_facts(expense):
    paid_at_entry = _expense_paid_at_entry(expense)
    return {
        "label": f"Expense #{expense.pk}",
        "is_bank": expense.payment_method == BANK and paid_at_entry > 0,
        "amount": paid_at_entry,
        "date": expense.date,
        "party": expense.display_title,
        "bank": _bank_name(expense.bank_account),
        "direction": "Paid from",
        "home": reverse("expense_list"),
    }


def _expense_payment_facts(payment):
    return {
        "label": f"Expense payment #{payment.pk}",
        "is_bank": payment.payment_method == BANK,
        "amount": payment.amount,
        "date": payment.date,
        "party": payment.expense.display_title,
        "bank": payment.bank_reference if payment.payment_method == BANK else "",
        "direction": "Paid from",
        "home": reverse("expense_list"),
    }


def _sale_return_facts(sale_return):
    return {
        "label": f"Sale return #{sale_return.pk}",
        "is_bank": sale_return.refund_method == BANK,
        "amount": sale_return.total_amount,
        "date": _local_date(sale_return.created_at),
        "party": sale_return.customer.name if sale_return.customer_id else "Walk-in customer",
        "bank": _bank_name(sale_return.bank_account),
        "direction": "Refunded from",
        "home": reverse("invoice", args=[sale_return.sale_id]),
    }


def _purchase_return_facts(purchase_return):
    return {
        "label": f"Purchase return #{purchase_return.pk}",
        "is_bank": purchase_return.settlement_method == BANK,
        "amount": purchase_return.total_amount,
        "date": _local_date(purchase_return.created_at),
        "party": purchase_return.supplier.name if purchase_return.supplier_id else "Cash purchase",
        "bank": _bank_name(purchase_return.bank_account),
        "direction": "Refund received into",
        "home": reverse("purchase_detail", args=[purchase_return.purchase_id]),
    }


def _transfer_facts(transfer):
    out_of_bank = transfer.from_account == BANK
    into_bank = transfer.to_account == BANK
    return {
        "label": f"Transfer #{transfer.pk}",
        "is_bank": out_of_bank or into_bank,
        "amount": transfer.amount,
        "date": transfer.date,
        "party": f"{transfer.from_account_title} to {transfer.to_account_title}",
        "bank": transfer.to_bank_reference if into_bank else transfer.from_bank_reference,
        "direction": "Moved into" if into_bank else "Moved out of",
        "home": reverse("money_desk"),
    }


def _builty_facts(builty):
    return {
        "label": builty.display_number,
        "is_bank": builty.payment_method == BANK,
        "amount": builty.amount,
        "date": builty.date,
        "party": builty.transporter.name if builty.transporter_id else "Transporter",
        "bank": _bank_name(builty.bank_account),
        "direction": "Paid from",
        "home": reverse("builty_detail", args=[builty.pk]),
    }


def _capital_facts(entry):
    return {
        "label": f"Funds introduced #{entry.pk}",
        "is_bank": entry.deposit_to == BANK,
        "amount": entry.amount,
        "date": entry.date,
        "party": entry.source_name,
        "bank": entry.bank_reference if entry.deposit_to == BANK else "",
        "direction": "Received into",
        "home": reverse("money_desk"),
    }


@dataclass(frozen=True)
class ReceiptKind:
    slug: str
    model_label: str
    # Access areas of pos.services.access; holding any one of them opens the receipt.
    modules: tuple
    # record -> label, is_bank, amount, date, party, bank, direction, home (see describe()).
    facts: Callable
    # The rows that can be bank payments, as a database filter. It may let in a few more than
    # facts()["is_bank"] accepts (never fewer); it keeps long ledgers from loading every record.
    maybe_bank: Q
    related: tuple = ()
    prepare: Callable | None = None

    @property
    def model(self):
        return apps.get_model(self.model_label)

    @property
    def on_a_sale(self) -> bool:
        return self.slug in ("sale", "sale-return")


KINDS = (
    ReceiptKind(
        "sale", "pos.Sale", ("pos", "reports"), _sale_facts,
        Q(payment_method=BANK, is_credit=False, paid_amount__gt=0),
        related=("customer", "bank_account"),
    ),
    ReceiptKind(
        "purchase", "pos.Purchase", ("purchases",), _purchase_facts,
        Q(payment_method=BANK, paid_amount__gt=0),
        related=("supplier", "bank_account"),
    ),
    ReceiptKind(
        "builty", "pos.Builty", ("purchases",), _builty_facts,
        Q(payment_method=BANK),
        related=("transporter", "bank_account"),
    ),
    ReceiptKind(
        "settlement", "accounts.Settlement", ("parties",), _settlement_facts,
        Q(payment_method=BANK),
        related=("bank_account",),
    ),
    ReceiptKind(
        "expense", "accounts.Expense", ("expenses",), _expense_facts,
        Q(payment_method=BANK, paid_amount__gt=0),
        related=("category", "bank_account"), prepare=_prepare_expenses,
    ),
    ReceiptKind(
        "expense-payment", "accounts.ExpensePayment", ("expenses", "accounts"), _expense_payment_facts,
        Q(payment_method=BANK),
        related=("expense", "expense__category", "bank_account"),
    ),
    ReceiptKind(
        "sale-return", "pos.SaleReturn", ("pos", "reports"), _sale_return_facts,
        Q(refund_method=BANK),
        related=("sale", "customer", "bank_account"),
    ),
    ReceiptKind(
        "purchase-return", "pos.PurchaseReturn", ("purchases",), _purchase_return_facts,
        Q(settlement_method=BANK),
        related=("supplier", "bank_account"),
    ),
    ReceiptKind(
        "transfer", "accounts.MoneyTransfer", ("accounts",), _transfer_facts,
        Q(from_account=BANK) | Q(to_account=BANK),
        related=("from_bank_account", "to_bank_account"),
    ),
    ReceiptKind(
        "capital", "accounts.CapitalEntry", ("accounts",), _capital_facts,
        Q(deposit_to=BANK),
        related=("bank_account",),
    ),
)
KIND_BY_SLUG = {kind.slug: kind for kind in KINDS}
# Voucher.ref_model is the class name of the record a voucher was posted from.
KIND_BY_MODEL_NAME = {kind.model_label.split(".")[1]: kind for kind in KINDS}


def kind_for_slug(slug):
    return KIND_BY_SLUG.get(slug)


def kind_of(record) -> ReceiptKind:
    kind = KIND_BY_MODEL_NAME.get(record.__class__.__name__)
    if kind is None or not isinstance(record, kind.model):
        raise ValueError(f"{record.__class__.__name__} records do not carry bank receipts.")
    return kind


def record_queryset(kind: ReceiptKind):
    return kind.model.objects.select_related(*kind.related)


def describe(record) -> dict:
    """label, is_bank, amount, date, party, bank, direction and home url of the payment."""
    kind = kind_of(record)
    facts = kind.facts(record)
    facts["kind"] = kind
    facts["url"] = receipt_url(record)
    return facts


def is_bank_payment(record) -> bool:
    return bool(kind_of(record).facts(record)["is_bank"])


def receipt_url(record) -> str:
    return reverse("bank_receipt", args=[kind_of(record).slug, record.pk])


# ------------------------------------------------------------------------ reading

def _content_type(model):
    return ContentType.objects.get_for_model(model, for_concrete_model=True)


def active_receipts(record):
    return BankReceipt.objects.filter(
        content_type=_content_type(record.__class__),
        object_id=record.pk,
        removed_at__isnull=True,
    )


def get_receipt(record):
    """The receipt in force for this record, or None."""
    if record is None or record.pk is None:
        return None
    return active_receipts(record).order_by("-uploaded_at", "-id").first()


def has_receipt(record) -> bool:
    return record is not None and record.pk is not None and active_receipts(record).exists()


# ------------------------------------------------------------------------ writing

def _actor(user):
    return user if getattr(user, "is_authenticated", False) else None


def _audit_metadata(record, receipt, **extra):
    kind = kind_of(record)
    return {
        "payment_kind": kind.slug,
        "payment_id": record.pk,
        "receipt_id": receipt.pk,
        "file_name": receipt.original_name,
        "mime_type": receipt.mime_type,
        "size_bytes": receipt.size_bytes,
        "sha256": receipt.sha256,
        **extra,
    }


@transaction.atomic
def attach_receipt(record, upload, *, actor=None, audit=True):
    """Put a receipt on a payment. Returns the new BankReceipt, or None when no file was given.

    A receipt already in force is replaced: it keeps its row and file and is marked removed.
    `audit=False` is for the moment the payment itself is recorded (its own entry says who
    and when); attaching later, replacing and removing are always written to the audit log.
    """
    if not upload:
        return None
    checked = check_upload(upload)
    kind = kind_of(record)
    actor = _actor(actor)
    label = kind.facts(record)["label"]

    replaced = list(active_receipts(record).select_for_update())
    now = timezone.now()
    for old in replaced:
        old.removed_at = now
        old.removed_by = actor
        old.save(update_fields=["removed_at", "removed_by"])

    receipt = BankReceipt(
        content_type=_content_type(record.__class__),
        object_id=record.pk,
        original_name=checked.original_name,
        mime_type=checked.mime_type,
        size_bytes=checked.size,
        sha256=checked.sha256,
        uploaded_by=actor,
    )
    try:
        receipt.file.save(f"receipt{checked.extension}", ContentFile(checked.data), save=False)
    except Exception as exc:  # a full disk locally, or whatever the configured remote storage raises
        logger.exception("Storing a bank receipt failed")
        raise ReceiptStorageError(STORAGE_MESSAGE) from exc
    receipt.save()

    if replaced:
        record_audit(
            action="bank_receipt_replaced",
            description=f"Bank receipt replaced on {label}.",
            actor=actor,
            target=record,
            metadata=_audit_metadata(
                record,
                receipt,
                replaced_receipt_ids=[old.pk for old in replaced],
                replaced_sha256=[old.sha256 for old in replaced],
            ),
        )
    elif audit:
        record_audit(
            action="bank_receipt_attached",
            description=f"Bank receipt attached to {label}.",
            actor=actor,
            target=record,
            metadata=_audit_metadata(record, receipt),
        )
    return receipt


def attach_optional_receipt(request, record, upload):
    """For payments that never demand a receipt (refunds, money desk), after they are saved.

    The entry stands even if the file cannot be stored; the user is told so and can add
    the receipt again from the entry's receipt page.
    """
    try:
        return attach_receipt(record, upload, actor=request.user, audit=False)
    except ReceiptStorageError:
        label = kind_of(record).facts(record)["label"]
        messages.warning(
            request,
            f"{label} is saved, but its bank receipt could not be stored. Open the entry and attach the receipt again.",
        )
        return None


@transaction.atomic
def remove_receipt(record, *, actor=None) -> bool:
    """Take the receipt off a payment. The row and the file stay for the audit trail."""
    actor = _actor(actor)
    removed = list(active_receipts(record).select_for_update())
    if not removed:
        return False
    now = timezone.now()
    label = kind_of(record).facts(record)["label"]
    for receipt in removed:
        receipt.removed_at = now
        receipt.removed_by = actor
        receipt.save(update_fields=["removed_at", "removed_by"])
        record_audit(
            action="bank_receipt_removed",
            description=f"Bank receipt removed from {label}.",
            actor=actor,
            target=record,
            metadata=_audit_metadata(record, receipt),
        )
    return True


# ------------------------------------------------------------------------- access

def _module_access(user, kind: ReceiptKind, level) -> bool:
    from pos.services.access import user_can_access_module

    return any(user_can_access_module(user, module, level) for module in kind.modules)


def _sale_of(record, kind: ReceiptKind):
    if not kind.on_a_sale:
        return None
    return record if kind.slug == "sale" else record.sale


def can_view_receipt(user, record) -> bool:
    """The same access the payment itself needs to be opened."""
    from pos.services.access import ACCESS_VIEW, user_can_access_sale

    kind = kind_of(record)
    if not _module_access(user, kind, ACCESS_VIEW):
        return False
    sale = _sale_of(record, kind)
    return sale is None or user_can_access_sale(user, sale)


def can_attach_receipt(user, record) -> bool:
    """Adding the proof to a bank payment that has none: whoever can open the payment.

    The proof often arrives after the sale, and adding it changes no figure. Tighten this
    to can_change_receipt if view-only roles should not be able to add one.
    """
    return can_view_receipt(user, record)


def can_change_receipt(user, record) -> bool:
    """Replacing or removing a receipt needs manage access to the payment's area."""
    from pos.services.access import (
        ACCESS_MANAGE,
        user_can_access_module,
        user_can_access_sale,
    )

    kind = kind_of(record)
    sale = _sale_of(record, kind)
    if sale is not None:
        # A reports-only role can read invoices; changing their proof belongs to the till's managers.
        return user_can_access_module(user, "pos", ACCESS_MANAGE) and user_can_access_sale(user, sale)
    return _module_access(user, kind, ACCESS_MANAGE)


# ------------------------------------------------------- receipts behind rows of a list

def receipt_marks(refs, user=None) -> dict:
    """{(model name, id): mark} for the records that are bank payments or already carry a receipt.

    `refs` are (model class name, id) pairs, the way a Voucher points at its source
    (ref_model, ref_id). A mark is {"url", "attached", "label"}; `url` is the receipt page.
    With a user, records whose area that user cannot open are left out.
    """
    from pos.services.access import ACCESS_VIEW

    wanted = {}
    for model_name, pk in refs:
        kind = KIND_BY_MODEL_NAME.get(model_name or "")
        if kind is not None and pk:
            wanted.setdefault(kind, set()).add(int(pk))

    marks = {}
    for kind, ids in wanted.items():
        if user is not None and not _module_access(user, kind, ACCESS_VIEW):
            continue
        model_name = kind.model.__name__
        ids = sorted(ids)
        for start in range(0, len(ids), MARK_CHUNK):
            chunk = ids[start:start + MARK_CHUNK]
            attached = set(
                BankReceipt.objects.filter(
                    content_type=_content_type(kind.model),
                    object_id__in=chunk,
                    removed_at__isnull=True,
                ).values_list("object_id", flat=True)
            )
            records = list(record_queryset(kind).filter(pk__in=chunk).filter(kind.maybe_bank | Q(pk__in=attached)))
            if kind.prepare is not None:
                kind.prepare(records)
            for record in records:
                facts = kind.facts(record)
                has_one = record.pk in attached
                if not has_one and not facts["is_bank"]:
                    continue
                marks[(model_name, record.pk)] = {
                    "url": reverse("bank_receipt", args=[kind.slug, record.pk]),
                    "attached": has_one,
                    "label": facts["label"],
                }
    return marks


def mark_records(records, user=None):
    """Set `.bank_receipt_mark` (a mark or None) on a page of records of one model."""
    records = list(records)
    if not records:
        return records
    model_name = records[0].__class__.__name__
    marks = receipt_marks([(model_name, record.pk) for record in records], user=user)
    for record in records:
        record.bank_receipt_mark = marks.get((model_name, record.pk))
    return records


def mark_ledger_rows(rows, user=None):
    """Ledger rows (dicts holding a "voucher") gain row["receipt"]: a mark or None."""
    rows = list(rows)
    marks = receipt_marks([(row["voucher"].ref_model, row["voucher"].ref_id) for row in rows], user=user)
    for row in rows:
        row["receipt"] = marks.get((row["voucher"].ref_model, row["voucher"].ref_id))
    return rows


def mark_statement_rows(rows, user=None):
    """Party statement rows as the API sends them ("ref": "Settlement#12") gain row["receipt"]."""
    refs = {}
    for index, row in enumerate(rows):
        model_name, _, pk = str(row.get("ref") or "").partition("#")
        if pk.isdigit():
            refs[index] = (model_name, int(pk))
    marks = receipt_marks(refs.values(), user=user)
    for index, row in enumerate(rows):
        mark = marks.get(refs.get(index))
        row["receipt"] = {"url": mark["url"], "attached": mark["attached"]} if mark else None
    return rows
