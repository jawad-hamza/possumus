"""
Simple (direct) cash flow: what moved through cash and the bank ledgers,
grouped by the kind of transaction that moved it.
"""

from django.db.models import Q, Sum
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine
from accounts.services.report_utils import ZERO, movement_totals, opening_sides, parse_report_range, to_money

# Journals cover very different things; the source document says which.
REF_MODEL_LABELS = {
    "CapitalEntry": "Owner funds introduced",
    "MoneyTransfer": "Transfers between cash and bank",
    "SaleReturn": "Sale returns",
    "PurchaseReturn": "Purchase returns",
    "StockAdjustment": "Stock adjustments",
    "ExpensePayment": "Expense bill payments",
    "Settlement": None,  # receipt/payment voucher types already say it
}
VOUCHER_TYPE_LABELS = {
    Voucher.SALE: "Sales",
    Voucher.PURCHASE: "Purchases",
    Voucher.RECEIPT: "Customer receipts",
    Voucher.PAYMENT: "Supplier payments",
    Voucher.EXPENSE: "Expenses",
    Voucher.LOAN: "Loans",
    Voucher.JOURNAL: "Other journal entries",
}


def money_accounts():
    """Cash, the legacy Bank account and every bank ledger, active or not: history still counts."""
    return Account.objects.filter(
        Q(title="Cash") | Q(title="Bank") | Q(bank_ledger__isnull=False),
        type=Account.ASSET,
    ).select_related("bank_ledger").distinct()


def _group_label(voucher_type, ref_model):
    label = REF_MODEL_LABELS.get(ref_model or "")
    return label or VOUCHER_TYPE_LABELS.get(voucher_type, (voucher_type or "Other").title())


def get_cash_flow(start_date=None, end_date=None, all_time=False):
    """Defaults to the current month unless all_time is asked for."""
    start_date, end_date = parse_report_range(start_date, end_date)
    if start_date is None and end_date is None and not all_time:
        end_date = timezone.localdate()
        start_date = end_date.replace(day=1)

    accounts = list(money_accounts())
    account_ids = [account.id for account in accounts]

    opening = ZERO
    for account in accounts:
        opening_debit, opening_credit = opening_sides(account)
        opening += opening_debit - opening_credit
    if start_date:
        before = movement_totals(before=start_date)
        for account_id in account_ids:
            debit, credit = before.get(account_id, (ZERO, ZERO))
            opening += debit - credit

    lines = VoucherLine.objects.filter(account_id__in=account_ids)
    if start_date:
        lines = lines.filter(voucher__date__gte=start_date)
    if end_date:
        lines = lines.filter(voucher__date__lte=end_date)

    groups = {}
    for row in lines.values("voucher__voucher_type", "voucher__ref_model").annotate(money_in=Sum("debit"), money_out=Sum("credit")):
        label = _group_label(row["voucher__voucher_type"], row["voucher__ref_model"])
        group = groups.setdefault(label, {"label": label, "money_in": ZERO, "money_out": ZERO})
        group["money_in"] += to_money(row["money_in"])
        group["money_out"] += to_money(row["money_out"])

    rows = []
    total_in = total_out = ZERO
    for group in groups.values():
        group["net"] = group["money_in"] - group["money_out"]
        total_in += group["money_in"]
        total_out += group["money_out"]
        rows.append(group)
    rows.sort(key=lambda group: group["label"])

    by_account = []
    period = movement_totals(start=start_date, end=end_date)
    for account in accounts:
        debit, credit = period.get(account.id, (ZERO, ZERO))
        if debit == 0 and credit == 0:
            continue
        bank = getattr(account, "bank_ledger", None)
        by_account.append({
            "account_id": account.id,
            "label": bank.display_name if bank else account.title,
            "money_in": debit,
            "money_out": credit,
            "net": debit - credit,
        })

    return {
        "rows": rows,
        "by_account": by_account,
        "start_date": start_date,
        "end_date": end_date,
        "opening_balance": opening,
        "total_in": total_in,
        "total_out": total_out,
        "net_change": total_in - total_out,
        "closing_balance": opening + total_in - total_out,
    }
