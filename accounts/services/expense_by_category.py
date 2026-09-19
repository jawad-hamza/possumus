from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from accounts.models import Expense
from accounts.services.report_utils import ZERO, parse_report_range, to_money


def get_expense_by_category(start_date=None, end_date=None, all_time=False):
    """Recorded expense bills grouped by expense type. Defaults to the current month unless all_time is asked for."""
    start_date, end_date = parse_report_range(start_date, end_date)
    if start_date is None and end_date is None and not all_time:
        end_date = timezone.localdate()
        start_date = end_date.replace(day=1)

    expenses = Expense.objects.all()
    if start_date:
        expenses = expenses.filter(date__gte=start_date)
    if end_date:
        expenses = expenses.filter(date__lte=end_date)

    grouped = (
        expenses.values("category_id", "category__name")
        .annotate(bills=Count("id"), amount=Sum("amount"), paid=Sum("paid_amount"))
        .order_by("-amount", "category__name")
    )

    rows = []
    total_amount = total_paid = ZERO
    total_bills = 0
    for group in grouped:
        amount = to_money(group["amount"])
        paid = to_money(group["paid"])
        rows.append({
            "category_id": group["category_id"],
            "category": group["category__name"],
            "bills": group["bills"],
            "amount": amount,
            "paid": paid,
            "due": amount - paid,
        })
        total_amount += amount
        total_paid += paid
        total_bills += group["bills"]

    for row in rows:
        row["share"] = (row["amount"] * Decimal("100") / total_amount).quantize(Decimal("0.1")) if total_amount else Decimal("0.0")

    return {
        "rows": rows,
        "start_date": start_date,
        "end_date": end_date,
        "total_bills": total_bills,
        "total_amount": total_amount,
        "total_paid": total_paid,
        "total_due": total_amount - total_paid,
    }
