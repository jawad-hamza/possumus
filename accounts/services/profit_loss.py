from django.db.models import Sum
from accounts.models import Account, VoucherLine


def get_profit_and_loss(start_date=None, end_date=None):
    """
    Returns Profit & Loss statement.
    """

    def line_total(account):
        qs = VoucherLine.objects.filter(account=account)

        if start_date:
            qs = qs.filter(voucher__date__gte=start_date)
        if end_date:
            qs = qs.filter(voucher__date__lte=end_date)

        sums = qs.aggregate(
            debit=Sum("debit"),
            credit=Sum("credit"),
        )

        debit = sums["debit"] or 0
        credit = sums["credit"] or 0

        # Income: credit - debit
        if account.type == Account.INCOME:
            return credit - debit

        # Expense: debit - credit
        return debit - credit

    income_rows = []
    expense_rows = []

    total_income = 0
    total_expense = 0

    accounts = Account.objects.filter(is_active=True)

    for acc in accounts:
        if acc.type not in (Account.INCOME, Account.EXPENSE):
            continue

        amount = line_total(acc)

        if amount == 0:
            continue

        if acc.type == Account.INCOME:
            income_rows.append({
                "account": acc.title,
                "amount": amount
            })
            total_income += amount

        else:
            expense_rows.append({
                "account": acc.title,
                "amount": amount
            })
            total_expense += amount

    return {
        "income": income_rows,
        "expenses": expense_rows,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_profit": total_income - total_expense,
    }
