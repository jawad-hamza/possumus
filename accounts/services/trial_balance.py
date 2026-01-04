# from django.db.models import Sum
# from accounts.models import Account, VoucherLine


# def get_trial_balance():
#     """
#     Returns trial balance rows and totals.
#     """
#     rows = []
#     total_debit = 0
#     total_credit = 0

#     accounts = Account.objects.filter(is_active=True).order_by("type", "title")

#     for acc in accounts:
#         sums = VoucherLine.objects.filter(account=acc).aggregate(
#             debit=Sum("debit"),
#             credit=Sum("credit"),
#         )

#         debit = sums["debit"] or 0
#         credit = sums["credit"] or 0

#         # Skip accounts with zero movement
#         if debit == 0 and credit == 0:
#             continue

#         rows.append({
#             "account": acc.title,
#             "type": acc.type,
#             "debit": debit,
#             "credit": credit,
#         })

#         total_debit += debit
#         total_credit += credit

#     return {
#         "rows": rows,
#         "total_debit": total_debit,
#         "total_credit": total_credit,
#         "balanced": total_debit == total_credit,
#     }

from django.db.models import Sum
from accounts.models import Account, VoucherLine


def get_trial_balance():
    """
    Returns trial balance rows and totals.
    """
    rows = []
    total_debit = 0
    total_credit = 0

    accounts = Account.objects.filter(is_active=True).order_by("type", "title")

    for acc in accounts:
        sums = VoucherLine.objects.filter(account=acc).aggregate(
            debit=Sum("debit"),
            credit=Sum("credit"),
        )

        debit = sums["debit"] or 0
        credit = sums["credit"] or 0

        if debit == 0 and credit == 0:
            continue

        rows.append({
            "account_id": acc.id,
            "account": acc.title,
            "type": acc.type,
            "debit": debit,
            "credit": credit,
        })

        total_debit += debit
        total_credit += credit

    return {
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": total_debit == total_credit,
    }
