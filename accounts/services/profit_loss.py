from accounts.models import Account
from accounts.services.report_utils import ZERO, account_totals, parse_report_range


def get_profit_and_loss(start_date=None, end_date=None):
    """
    Profit & Loss for a period. An opening balance on an income/expense account
    belongs to the time before the first posting, so it only counts when the
    report is not limited by a start date.
    """
    start_date, end_date = parse_report_range(start_date, end_date)

    income_rows = []
    expense_rows = []
    total_income = ZERO
    total_expense = ZERO

    for account, debit, credit in account_totals(start=start_date, end=end_date, include_opening=start_date is None):
        if account.type == Account.INCOME:
            amount = credit - debit
            if amount == 0:
                continue
            income_rows.append({"account_id": account.id, "account": account.title, "amount": amount})
            total_income += amount
        elif account.type == Account.EXPENSE:
            amount = debit - credit
            if amount == 0:
                continue
            expense_rows.append({"account_id": account.id, "account": account.title, "amount": amount})
            total_expense += amount

    return {
        "income": income_rows,
        "expenses": expense_rows,
        "start_date": start_date,
        "end_date": end_date,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_profit": total_income - total_expense,
    }
