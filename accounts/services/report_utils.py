"""Shared building blocks for the accounting reports."""

import csv
import datetime
from decimal import Decimal

from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone

from accounts.models import Account, VoucherLine
from pos.models import quantize_money

ZERO = Decimal("0.00")
DEBIT_NORMAL_TYPES = (Account.ASSET, Account.EXPENSE)
OPENING_DIFFERENCE_LABEL = "Difference in opening balances"


def to_money(value):
    """
    Aggregates come back from SQLite as floats turned into long decimals
    (391053.1700000000). Two places keeps equality checks and CSV output exact.
    """
    return quantize_money(value or 0)


def parse_report_date(value):
    """A date, or None for anything that is not one. Reports never fail on a bad date."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def parse_report_range(start, end):
    """Both ends parsed; a reversed range is put the right way round."""
    start = parse_report_date(start)
    end = parse_report_date(end)
    if start and end and start > end:
        start, end = end, start
    return start, end


def opening_sides(account):
    """
    Account.opening_balance is stored on the account's natural side
    (assets/expenses debit, everything else credit); a negative value sits on
    the other side. Returns (debit, credit).
    """
    opening = account.opening_balance or ZERO
    if opening == 0:
        return ZERO, ZERO
    debit_side = account.type in DEBIT_NORMAL_TYPES
    if opening < 0:
        debit_side = not debit_side
        opening = -opening
    return (opening, ZERO) if debit_side else (ZERO, opening)


def natural_balance(account, debit, credit):
    if account.type in DEBIT_NORMAL_TYPES:
        return debit - credit
    return credit - debit


def movement_totals(*, start=None, end=None, before=None):
    """{account_id: (debit, credit)} for posted lines, in one query."""
    lines = VoucherLine.objects.all()
    if start:
        lines = lines.filter(voucher__date__gte=start)
    if end:
        lines = lines.filter(voucher__date__lte=end)
    if before:
        lines = lines.filter(voucher__date__lt=before)
    return {
        row["account_id"]: (to_money(row["debit"]), to_money(row["credit"]))
        for row in lines.values("account_id").annotate(debit=Sum("debit"), credit=Sum("credit"))
    }


def account_totals(*, start=None, end=None, include_opening=True):
    """
    Yields (account, debit, credit) for every account that has an opening
    balance or postings in the range. Inactive accounts are included on
    purpose: hiding an account that carries history unbalances the books.
    """
    movements = movement_totals(start=start, end=end)
    for account in Account.objects.order_by("type", "title"):
        debit, credit = movements.get(account.id, (ZERO, ZERO))
        if include_opening:
            opening_debit, opening_credit = opening_sides(account)
            debit += opening_debit
            credit += opening_credit
        if debit == 0 and credit == 0:
            continue
        yield account, debit, credit


def opening_difference():
    """Opening debits minus opening credits across the chart; zero when openings were entered in full."""
    debit = credit = ZERO
    for account in Account.objects.exclude(opening_balance=0):
        opening_debit, opening_credit = opening_sides(account)
        debit += opening_debit
        credit += opening_credit
    return debit - credit


def plain_amount(value):
    """2dp number without symbol or separators, so spreadsheets read it as a number."""
    return str(to_money(value))


def _safe_cell(value):
    """Names and narrations are user text; stop a spreadsheet from running them as formulas."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        try:
            Decimal(value)
        except ArithmeticError:
            return "'" + value
    return "" if value is None else value


def csv_response(filename, header, rows):
    """
    UTF-8 with a BOM (what Python calls utf-8-sig) so Excel detects the encoding
    and shows non-Latin names correctly. The BOM is written once by hand: the
    utf-8-sig codec would add one on every write().
    """
    stamp = timezone.localdate().strftime("%Y%m%d")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}-{stamp}.csv"'
    response.write(chr(0xFEFF))
    writer = csv.writer(response)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe_cell(cell) for cell in row])
    return response
