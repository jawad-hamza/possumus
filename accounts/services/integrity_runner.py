from decimal import Decimal
from django.db.models import Sum
from django.db.models.functions import Coalesce

from accounts.models import Account, VoucherLine, PartyLedgerEntry
from accounts.services.integrity import check_ar_integrity
from accounts.services.trial_balance import get_trial_balance


# -------------------------------------------------
# INTERNAL HELPERS
# -------------------------------------------------

def check_ap_integrity() -> dict:
    """
    Ensures Accounts Payable control account equals
    party balances coming ONLY from AP-linked vouchers.
    """

    ap = Account.objects.get(title="Accounts Payable", is_active=True)

    ap_totals = VoucherLine.objects.filter(
        account=ap
    ).aggregate(
        debit=Coalesce(Sum("debit"), Decimal("0")),
        credit=Coalesce(Sum("credit"), Decimal("0")),
    )

    # Liability → credit positive
    ap_balance = ap_totals["debit"] - ap_totals["credit"]

    ap_vouchers = VoucherLine.objects.filter(
        account=ap
    ).values_list("voucher_id", flat=True)

    party_totals = PartyLedgerEntry.objects.filter(
        voucher_id__in=ap_vouchers
    ).aggregate(
        debit=Coalesce(Sum("debit"), Decimal("0")),
        credit=Coalesce(Sum("credit"), Decimal("0")),
    )

    party_ap_balance = party_totals["credit"] - party_totals["debit"]

    return {
        "ap_balance": ap_balance,
        "party_balance": party_ap_balance,
        "is_ok": abs(ap_balance) == party_ap_balance,
    }


# -------------------------------------------------
# MASTER INTEGRITY RUNNER
# -------------------------------------------------

def run_integrity_checks() -> dict:
    """
    Runs all accounting integrity checks.

    Verifies:
    - AR control == customer party balances
    - AP control == supplier party balances
    - Trial balance is mathematically balanced
    """

    ar = check_ar_integrity()
    ap = check_ap_integrity()
    tb = get_trial_balance()

    return {
        "accounts_receivable_ok": ar["is_ok"],
        "accounts_payable_ok": ap["is_ok"],
        "trial_balance_ok": tb["balanced"],
        "details": {
            "accounts_receivable": ar,
            "accounts_payable": ap,
            "trial_balance": tb,
        }
    }