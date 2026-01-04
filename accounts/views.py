from django.shortcuts import render, get_object_or_404
from django.contrib.contenttypes.models import ContentType
from django.db.models import Sum

from accounts.models import Account, Voucher, VoucherLine, PartyLedgerEntry
from accounts.services.trial_balance import get_trial_balance
from accounts.services.profit_loss import get_profit_and_loss
from accounts.services.balance_sheet import get_balance_sheet


def _sum_account(title: str):
    """
    Returns account balance based on type rules:
    Assets/Expenses: DR-CR
    Liabilities/Equity/Income: CR-DR
    """
    try:
        acc = Account.objects.get(title=title, is_active=True)
    except Account.DoesNotExist:
        return 0

    sums = VoucherLine.objects.filter(account=acc).aggregate(
        debit=Sum("debit"),
        credit=Sum("credit"),
    )
    debit = sums["debit"] or 0
    credit = sums["credit"] or 0

    if acc.type in (Account.ASSET, Account.EXPENSE):
        return debit - credit
    return credit - debit


def accounts_home(request):
    tb = get_trial_balance()
    pl = get_profit_and_loss()
    bs = get_balance_sheet()

    # Core balances (safe even if account missing)
    cash_balance = _sum_account("Cash")
    bank_balance = _sum_account("Bank")
    ar_balance = _sum_account("Accounts Receivable")
    ap_balance = _sum_account("Accounts Payable")

    recent_vouchers = (
        Voucher.objects.all()
        .order_by("-date", "-id")[:10]
    )

    context = {
        "trial_balance": tb,
        "profit_loss": pl,
        "balance_sheet": bs,
        "cash_balance": cash_balance,
        "bank_balance": bank_balance,
        "ar_balance": ar_balance,
        "ap_balance": ap_balance,
        "recent_vouchers": recent_vouchers,
    }
    return render(request, "accounts/dashboard.html", context)


def trial_balance_view(request):
    context = get_trial_balance()
    return render(request, "accounts/trial_balance.html", context)


def profit_loss_view(request):
    start = request.GET.get("start")
    end = request.GET.get("end")
    context = get_profit_and_loss(start_date=start or None, end_date=end or None)
    return render(request, "accounts/profit_loss.html", context)


def balance_sheet_view(request):
    as_of = request.GET.get("as_of")
    context = get_balance_sheet(as_of_date=as_of or None)
    return render(request, "accounts/balance_sheet.html", context)


def account_ledger(request, account_id):
    account = get_object_or_404(Account, id=account_id)

    lines = (
        VoucherLine.objects
        .filter(account=account)
        .select_related("voucher")
        .order_by("voucher__date", "voucher__id")
    )

    running = 0
    ledger = []

    for line in lines:
        if line.debit > 0:
            running += line.debit
        else:
            running -= line.credit

        ledger.append({
            "date": line.voucher.date,
            "voucher": line.voucher,
            "debit": line.debit,
            "credit": line.credit,
            "balance": abs(running),
            "drcr": "Dr" if running >= 0 else "Cr",
        })

    return render(request, "accounts/account_ledger.html", {
        "account": account,
        "ledger": ledger,
    })


def party_statement(request, model, pk):
    ct = ContentType.objects.get(model=model)

    entries = (
        PartyLedgerEntry.objects
        .filter(content_type=ct, object_id=pk)
        .select_related("voucher")
        .order_by("voucher__date", "voucher__id")
    )

    running = 0
    rows = []

    for e in entries:
        running += e.debit
        running -= e.credit

        rows.append({
            "date": e.voucher.date,
            "voucher": e.voucher,
            "debit": e.debit,
            "credit": e.credit,
            "balance": abs(running),
            "drcr": "Dr" if running >= 0 else "Cr",
        })

    return render(request, "accounts/party_ledger.html", {
        "party": entries.first().party if entries else None,
        "rows": rows,
    })
