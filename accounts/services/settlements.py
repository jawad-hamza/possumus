from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from accounts.models import Account, Settlement, Voucher, VoucherLine
from accounts.services.cash_bank import resolve_money_account
from accounts.services.chart_of_accounts import get_core_account
from accounts.services.party_ledger import post_party_ledger


def _acc(title: str) -> Account:
    return get_core_account(title)


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount") from exc


def _money_label(settlement: Settlement) -> str:
    if settlement.payment_method == Settlement.CASH:
        return "Cash"
    if settlement.bank_account_id:
        return settlement.bank_account.display_name
    return "Bank"


@transaction.atomic
def post_settlement(settlement: Settlement, *, actor=None) -> Voucher:
    if settlement.party is None:
        raise ValueError("Settlement.party is required")

    amount = _to_decimal(settlement.amount)
    if amount <= 0:
        raise ValueError("Settlement amount must be positive")

    if getattr(settlement, "voucher_id", None):
        return settlement.voucher

    money = resolve_money_account(settlement.payment_method, settlement.bank_account)
    ar = _acc("Accounts Receivable")
    ap = _acc("Accounts Payable")

    if settlement.settlement_type == Settlement.RECEIPT:
        vtype = Voucher.RECEIPT
        narration = f"Receipt - {settlement.party} into {_money_label(settlement)}"
    elif settlement.settlement_type == Settlement.PAYMENT:
        vtype = Voucher.PAYMENT
        narration = f"Payment - {settlement.party} from {_money_label(settlement)}"
    else:
        raise ValueError("Invalid settlement type. Use RECEIPT or PAYMENT.")

    voucher = Voucher.objects.create(
        date=getattr(settlement, "date", None) or timezone.localdate(),
        voucher_type=vtype,
        narration=narration,
        ref_model="Settlement",
        ref_id=settlement.id,
        created_by=actor,
    )

    if settlement.settlement_type == Settlement.RECEIPT:
        VoucherLine.objects.create(voucher=voucher, account=money, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=ar, credit=amount)
        post_party_ledger(voucher=voucher, party=settlement.party, debit=Decimal("0"), credit=amount)
    else:
        VoucherLine.objects.create(voucher=voucher, account=ap, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=money, credit=amount)
        post_party_ledger(voucher=voucher, party=settlement.party, debit=amount, credit=Decimal("0"))

    voucher.clean()
    settlement.voucher = voucher
    settlement.save(update_fields=["voucher"])
    return voucher


@transaction.atomic
def create_receipt(*, party, amount, payment_method, bank_account=None, date=None, actor=None) -> Voucher:
    settlement = Settlement.objects.create(
        settlement_type=Settlement.RECEIPT,
        payment_method=payment_method,
        bank_account=bank_account,
        date=date or timezone.localdate(),
        party=party,
        amount=_to_decimal(amount),
    )
    return post_settlement(settlement, actor=actor)


@transaction.atomic
def create_payment(*, party, amount, payment_method, bank_account=None, date=None, actor=None) -> Voucher:
    settlement = Settlement.objects.create(
        settlement_type=Settlement.PAYMENT,
        payment_method=payment_method,
        bank_account=bank_account,
        date=date or timezone.localdate(),
        party=party,
        amount=_to_decimal(amount),
    )
    return post_settlement(settlement, actor=actor)
