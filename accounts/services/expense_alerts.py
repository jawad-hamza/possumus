from calendar import monthrange
from datetime import date

from django.db.models import Q
from django.utils import timezone

from accounts.models import Expense, ExpenseCategory


def cycle_due_date(category, year, month):
    if not category.default_due_day:
        return None
    last_day = monthrange(year, month)[1]
    return date(year, month, min(int(category.default_due_day), last_day))


def expense_exists_for_cycle(category, due_date):
    if not due_date:
        return False
    return Expense.objects.filter(category=category).filter(
        Q(due_date__year=due_date.year, due_date__month=due_date.month)
        | Q(due_date__isnull=True, date__year=due_date.year, date__month=due_date.month)
    ).exists()


def expense_initial_for_category(category, today=None):
    today = today or timezone.localdate()
    initial = {
        "category": category.id,
        "date": today,
    }
    due_date = None
    if category.is_recurring and category.default_due_day:
        due_date = cycle_due_date(category, today.year, today.month)
        if due_date:
            initial["due_date"] = due_date
            initial["title"] = f"{due_date.strftime('%B')} {category.name}"
    if category.expense_pattern == ExpenseCategory.MONTHLY_FIXED and category.default_amount is not None:
        initial["amount"] = category.default_amount
    return initial


def monthly_bill_alerts(today=None):
    today = today or timezone.localdate()
    categories = ExpenseCategory.objects.filter(
        is_active=True,
        expense_pattern__in=[ExpenseCategory.MONTHLY_FIXED, ExpenseCategory.MONTHLY_VARIABLE],
    ).order_by("default_due_day", "name")

    reminders = []
    for category in categories:
        due_date = cycle_due_date(category, today.year, today.month)
        if not due_date:
            continue
        if expense_exists_for_cycle(category, due_date):
            continue

        alert_days = int(category.alert_days_before or 7)
        days_until_due = (due_date - today).days
        if days_until_due > alert_days:
            continue

        if days_until_due < 0:
            status = "overdue"
            timing_label = f"{abs(days_until_due)} day{'s' if abs(days_until_due) != 1 else ''} overdue"
        elif days_until_due == 0:
            status = "today"
            timing_label = "Due today"
        else:
            status = "due_soon"
            timing_label = f"Due in {days_until_due} day{'s' if days_until_due != 1 else ''}"

        is_fixed = category.expense_pattern == ExpenseCategory.MONTHLY_FIXED
        prompt = "Record this month's bill now." if is_fixed else "Enter this month's bill amount now."
        reminders.append(
            {
                "category": category,
                "due_date": due_date,
                "days_until_due": days_until_due,
                "status": status,
                "timing_label": timing_label,
                "prompt": prompt,
                "title_hint": f"{due_date.strftime('%B')} {category.name}",
                "amount_hint": category.default_amount if is_fixed else None,
                "needs_amount": not is_fixed,
            }
        )

    priority = {"overdue": 0, "today": 1, "due_soon": 2}
    reminders.sort(key=lambda item: (priority[item["status"]], item["due_date"], item["category"].name.lower()))
    return reminders
