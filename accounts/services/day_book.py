from django.db.models import Sum
from django.utils import timezone

from accounts.models import Voucher, VoucherLine
from accounts.services.report_utils import parse_report_range, to_money


def get_day_book(start_date=None, end_date=None, voucher_type="", all_time=False):
    """Every voucher in the range, oldest first. Defaults to today unless all_time is asked for."""
    start_date, end_date = parse_report_range(start_date, end_date)
    if start_date is None and end_date is None and not all_time:
        start_date = end_date = timezone.localdate()

    vouchers = Voucher.objects.all()
    if start_date:
        vouchers = vouchers.filter(date__gte=start_date)
    if end_date:
        vouchers = vouchers.filter(date__lte=end_date)

    valid_types = dict(Voucher.VOUCHER_TYPE_CHOICES)
    voucher_type = voucher_type if voucher_type in valid_types else ""
    if voucher_type:
        vouchers = vouchers.filter(voucher_type=voucher_type)

    totals = VoucherLine.objects.filter(voucher__in=vouchers).aggregate(debit=Sum("debit"), credit=Sum("credit"))

    return {
        "vouchers": vouchers.select_related("created_by").prefetch_related("lines__account").order_by("date", "id"),
        "start_date": start_date,
        "end_date": end_date,
        "voucher_type": voucher_type,
        "voucher_types": Voucher.VOUCHER_TYPE_CHOICES,
        "voucher_count": vouchers.count(),
        "total_debit": to_money(totals["debit"]),
        "total_credit": to_money(totals["credit"]),
    }
