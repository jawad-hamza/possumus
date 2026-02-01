from decimal import Decimal
from django.db.models import Sum
from django.db.models.functions import Coalesce

from accounts.models import Account, VoucherLine, PartyLedgerEntry

# -------------------------------------------------
# INTERNAL HELPER
# -------------------------------------------------

def _account_balance(account: Account) -> Decimal:
    """
    Returns account balance using voucher lines.
    Debit - Credit
    """
    totals = VoucherLine.objects.filter(
        account=account
    ).aggregate(
        debit=Coalesce(Sum("debit"), Decimal("0")),
        credit=Coalesce(Sum("credit"), Decimal("0")),
    )

    return totals["debit"] - totals["credit"]


# -------------------------------------------------
# ACCOUNTS RECEIVABLE INTEGRITY
# -------------------------------------------------

def check_ar_integrity() -> dict:
    """
    Ensures Accounts Receivable control account equals
    party balances coming ONLY from AR-linked vouchers.
    """

    ar = Account.objects.get(title="Accounts Receivable", is_active=True)

    # AR control balance
    ar_totals = VoucherLine.objects.filter(
        account=ar
    ).aggregate(
        debit=Coalesce(Sum("debit"), Decimal("0")),
        credit=Coalesce(Sum("credit"), Decimal("0")),
    )
    ar_balance = ar_totals["debit"] - ar_totals["credit"]

    # Party balances ONLY from vouchers that hit AR
    ar_vouchers = VoucherLine.objects.filter(
        account=ar
    ).values_list("voucher_id", flat=True)

    party_totals = PartyLedgerEntry.objects.filter(
        voucher_id__in=ar_vouchers
    ).aggregate(
        debit=Coalesce(Sum("debit"), Decimal("0")),
        credit=Coalesce(Sum("credit"), Decimal("0")),
    )

    party_ar_balance = party_totals["debit"] - party_totals["credit"]

    return {
        "ar_balance": ar_balance,
        "party_balance": party_ar_balance,
        "is_ok": ar_balance == party_ar_balance,
    }