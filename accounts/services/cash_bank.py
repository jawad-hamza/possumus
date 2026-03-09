from decimal import Decimal

from django.db.models import Q, Sum

from accounts.models import Account, BankAccount, VoucherLine


ZERO = Decimal("0.00")


def ensure_cash_account() -> Account:
    account, _ = Account.objects.get_or_create(title="Cash", defaults={"type": Account.ASSET})
    if account.type != Account.ASSET:
        raise ValueError("Cash account must be an asset account")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def ensure_legacy_bank_account() -> Account:
    account, _ = Account.objects.get_or_create(title="Bank", defaults={"type": Account.ASSET})
    if account.type != Account.ASSET:
        raise ValueError("Bank account must be an asset account")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def resolve_money_account(payment_method: str, bank_account=None) -> Account:
    if payment_method == "cash":
        return ensure_cash_account()
    if payment_method == "bank":
        if bank_account is not None:
            return bank_account.account
        return ensure_legacy_bank_account()
    raise ValueError("Invalid payment method")


def bank_account_queryset():
    return BankAccount.objects.filter(is_active=True).select_related("account").order_by("name", "account_number")


def total_bank_balance() -> Decimal:
    totals = VoucherLine.objects.filter(
        Q(account__title="Bank") | Q(account__bank_ledger__isnull=False),
        account__is_active=True,
    ).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return (totals["debit"] or ZERO) - (totals["credit"] or ZERO)
