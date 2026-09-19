from accounts.models import Account
from accounts.services.profit_loss import get_profit_and_loss
from accounts.services.report_utils import (
    OPENING_DIFFERENCE_LABEL,
    ZERO,
    account_totals,
    natural_balance,
    opening_difference,
    parse_report_date,
)

CURRENT_EARNINGS_LABEL = "Current Earnings (profit to date)"


def get_balance_sheet(as_of_date=None):
    """Assets = Liabilities + Equity, where equity includes earnings up to the date."""
    as_of_date = parse_report_date(as_of_date)

    sections = {Account.ASSET: [], Account.LIABILITY: [], Account.EQUITY: []}
    totals = {Account.ASSET: ZERO, Account.LIABILITY: ZERO, Account.EQUITY: ZERO}

    for account, debit, credit in account_totals(end=as_of_date):
        if account.type not in sections:
            continue
        balance = natural_balance(account, debit, credit)
        if balance == 0:
            continue
        sections[account.type].append({"account_id": account.id, "account": account.title, "balance": balance})
        totals[account.type] += balance

    net_profit = get_profit_and_loss(end_date=as_of_date)["net_profit"]
    if net_profit != 0:
        sections[Account.EQUITY].append({"account_id": None, "account": CURRENT_EARNINGS_LABEL, "balance": net_profit})
        totals[Account.EQUITY] += net_profit

    # Openings entered one-sided are shown for what they are instead of
    # silently breaking the equation.
    difference = opening_difference()
    if difference != 0:
        sections[Account.EQUITY].append({"account_id": None, "account": OPENING_DIFFERENCE_LABEL, "balance": difference})
        totals[Account.EQUITY] += difference

    return {
        "assets": sections[Account.ASSET],
        "liabilities": sections[Account.LIABILITY],
        "equity": sections[Account.EQUITY],
        "as_of_date": as_of_date,
        "net_profit": net_profit,
        "opening_difference": difference,
        "total_assets": totals[Account.ASSET],
        "total_liabilities": totals[Account.LIABILITY],
        "total_equity": totals[Account.EQUITY],
        "balanced": totals[Account.ASSET] == (totals[Account.LIABILITY] + totals[Account.EQUITY]),
    }
