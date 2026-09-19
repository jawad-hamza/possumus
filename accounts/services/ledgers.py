"""
Account and party ledgers with a date range.

The running balance is computed by the database (window function) so a page
deep inside a long ledger still shows the right balance without loading every
earlier row.
"""

from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import DecimalField, F, Sum, Window

from accounts.models import PartyLedgerEntry, VoucherLine
from accounts.services.report_utils import ZERO, opening_sides, parse_report_range, to_money

LEDGER_ORDER = ("voucher__date", "voucher_id", "id")


def _running_window():
    return Window(
        expression=Sum(F("debit") - F("credit"), output_field=DecimalField(max_digits=14, decimal_places=2)),
        order_by=[F("voucher__date").asc(), F("voucher_id").asc(), F("id").asc()],
    )


def _signed_total(queryset):
    totals = queryset.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return to_money(totals["debit"]), to_money(totals["credit"])


def _ledger(base, *, opening, start, end):
    period = base
    if start:
        opening_debit, opening_credit = _signed_total(base.filter(voucher__date__lt=start))
        opening += opening_debit - opening_credit
        period = period.filter(voucher__date__gte=start)
    if end:
        period = period.filter(voucher__date__lte=end)

    total_debit, total_credit = _signed_total(period)
    closing = opening + total_debit - total_credit
    return {
        "entries": period.select_related("voucher").annotate(running=_running_window()).order_by(*LEDGER_ORDER),
        "start_date": start,
        "end_date": end,
        "opening": opening,
        "opening_balance": abs(opening),
        "opening_side": side_label(opening),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "closing": closing,
        "closing_balance": abs(closing),
        "closing_side": side_label(closing),
    }


def side_label(signed_amount) -> str:
    return "Dr" if signed_amount >= 0 else "Cr"


def ledger_rows(entries, opening):
    """Display rows for a page (or the whole ledger) of annotated entries."""
    rows = []
    for entry in entries:
        running = to_money(opening + Decimal(entry.running or 0))
        rows.append({
            "date": entry.voucher.date,
            "voucher": entry.voucher,
            "narration": entry.voucher.narration,
            "debit": entry.debit or ZERO,
            "credit": entry.credit or ZERO,
            "balance": abs(running),
            "drcr": side_label(running),
        })
    return rows


def get_account_ledger(account, start=None, end=None):
    start, end = parse_report_range(start, end)
    opening_debit, opening_credit = opening_sides(account)
    return _ledger(
        VoucherLine.objects.filter(account=account),
        opening=opening_debit - opening_credit,
        start=start,
        end=end,
    )


def get_party_ledger(party, start=None, end=None):
    start, end = parse_report_range(start, end)
    return _ledger(
        PartyLedgerEntry.objects.filter(
            content_type=ContentType.objects.get_for_model(party),
            object_id=party.pk,
        ),
        opening=ZERO,
        start=start,
        end=end,
    )
