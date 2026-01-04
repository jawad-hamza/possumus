from django.db.models import Sum
from accounts.models import Account, VoucherLine
from accounts.services.profit_loss import get_profit_and_loss


def get_balance_sheet(as_of_date=None):
    """
    Returns Balance Sheet as of a given date.
    """

    def account_balance(account):
        qs = VoucherLine.objects.filter(account=account)

        if as_of_date:
            qs = qs.filter(voucher__date__lte=as_of_date)

        sums = qs.aggregate(
            debit=Sum("debit"),
            credit=Sum("credit"),
        )

        debit = sums["debit"] or 0
        credit = sums["credit"] or 0

        # Assets & Expenses → debit - credit
        if account.type in (Account.ASSET, Account.EXPENSE):
            return debit - credit

        # Liabilities, Equity, Income → credit - debit
        return credit - debit

    assets = []
    liabilities = []
    equity = []

    total_assets = 0
    total_liabilities = 0
    total_equity = 0

    accounts = Account.objects.filter(is_active=True)

    for acc in accounts:
        if acc.type not in (Account.ASSET, Account.LIABILITY, Account.EQUITY):
            continue

        balance = account_balance(acc)

        if balance == 0:
            continue

        row = {"account": acc.title, "balance": balance}

        if acc.type == Account.ASSET:
            assets.append(row)
            total_assets += balance

        elif acc.type == Account.LIABILITY:
            liabilities.append(row)
            total_liabilities += balance

        else:
            equity.append(row)
            total_equity += balance

    # 🔹 Add Net Profit to Equity
    pl = get_profit_and_loss(end_date=as_of_date)
    net_profit = pl["net_profit"]

    if net_profit != 0:
        equity.append({
            "account": "Retained Earnings",
            "balance": net_profit
        })
        total_equity += net_profit

    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "balanced": total_assets == (total_liabilities + total_equity),
    }
