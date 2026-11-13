"""Template tags that show the bank receipt of a payment wherever that payment is listed.

    {% load bank_receipt_tags %}

    {% bank_receipt_panel sale %}             one payment on its own page: see the proof, or add a missing one
    {% mark_bank_receipts expenses %}         before a loop over records of one model...
    {% bank_receipt_link expense.bank_receipt_mark %}   ...then inside it
    {% mark_ledger_receipts rows %}           the same for ledger rows; then {% bank_receipt_link row.receipt %}

Nothing is drawn for a payment that did not go through a bank, or for a viewer who may not open it.
"""

from django import template

from accounts.services.bank_receipts import (
    ACCEPT_ATTRIBUTE,
    UPLOAD_FIELD,
    can_attach_receipt,
    can_view_receipt,
    describe,
    get_receipt,
    mark_ledger_rows,
    mark_records,
    max_upload_bytes,
    max_upload_label,
    receipts_required,
    upload_help_text,
)

register = template.Library()


def _request_user(context):
    request = context.get("request")
    return getattr(request, "user", None)


def _here(context):
    request = context.get("request")
    return request.get_full_path() if request is not None else ""


@register.inclusion_tag("accounts/_bank_receipt_panel.html", takes_context=True)
def bank_receipt_panel(context, record, layout="line"):
    user = _request_user(context)
    facts = describe(record)
    receipt = get_receipt(record)
    shown = (receipt is not None or facts["is_bank"]) and user is not None and can_view_receipt(user, record)
    return {
        "shown": shown,
        "layout": layout,
        "receipt": receipt,
        "url": facts["url"],
        "next": _here(context),
        "may_attach": shown and receipt is None and can_attach_receipt(user, record),
    }


@register.simple_tag(takes_context=True)
def mark_bank_receipts(context, records):
    """Give each record of the list `.bank_receipt_mark`; the whole page costs a few queries."""
    if records:
        mark_records(records, user=_request_user(context))
    return ""


@register.simple_tag(takes_context=True)
def mark_ledger_receipts(context, rows):
    """Give each ledger row (a dict with its voucher) row.receipt."""
    if rows:
        mark_ledger_rows(rows, user=_request_user(context))
    return ""


@register.inclusion_tag("accounts/_bank_receipt_link.html", takes_context=True)
def bank_receipt_link(context, mark):
    return {"mark": mark, "next": _here(context)}


@register.simple_tag
def bank_receipt_upload_rules():
    """What a hand-written upload control needs to know: the rule, the limit and the accepted types."""
    return {
        "required": receipts_required(),
        "max_bytes": max_upload_bytes(),
        "max_label": max_upload_label(),
        "accept": ACCEPT_ATTRIBUTE,
        "field": UPLOAD_FIELD,
        "help": upload_help_text(),
    }
