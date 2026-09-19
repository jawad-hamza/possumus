from accounts.services.report_utils import (
    OPENING_DIFFERENCE_LABEL,
    ZERO,
    account_totals,
    opening_difference,
    parse_report_date,
)


def get_trial_balance(as_of_date=None):
    """
    Trial balance as of a date (everything posted when no date is given).

    Debit and credit are gross totals including the account's opening balance.
    Opening balances that were entered one-sided are carried on their own row
    and reported as `opening_difference`, so `balanced` keeps meaning what it
    is used for: every posting has matching debits and credits.
    """
    as_of_date = parse_report_date(as_of_date)
    rows = []
    total_debit = ZERO
    total_credit = ZERO

    for account, debit, credit in account_totals(end=as_of_date):
        net = debit - credit
        rows.append({
            "account_id": account.id,
            "account": account.title,
            "type": account.type,
            "is_active": account.is_active,
            "debit": debit,
            "credit": credit,
            "balance_debit": net if net > 0 else ZERO,
            "balance_credit": -net if net < 0 else ZERO,
        })
        total_debit += debit
        total_credit += credit

    difference = opening_difference()
    if difference != 0:
        debit = -difference if difference < 0 else ZERO
        credit = difference if difference > 0 else ZERO
        rows.append({
            "account_id": None,
            "account": OPENING_DIFFERENCE_LABEL,
            "type": "equity",
            "is_active": True,
            "debit": debit,
            "credit": credit,
            "balance_debit": debit,
            "balance_credit": credit,
        })
        total_debit += debit
        total_credit += credit

    return {
        "rows": rows,
        "as_of_date": as_of_date,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "opening_difference": difference,
        "balanced": total_debit == total_credit,
    }
