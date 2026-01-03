from django.db import transaction
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine, Settlement
from accounts.services.party_ledger import post_party_ledger


def get_account(title: str) -> Account:
    return Account.objects.get(title=title, is_active=True)


@transaction.atomic
def post_settlement(settlement: Settlement) -> Voucher:
    cash = get_account("Cash")
    bank = get_account("Bank")
    ar = get_account("Accounts Receivable")
    ap = get_account("Accounts Payable")

    money = cash if settlement.payment_method == Settlement.CASH else bank

    # Create voucher
    v = Voucher.objects.create(
        date=settlement.date or timezone.now().date(),
        voucher_type=Voucher.RECEIPT if settlement.settlement_type == Settlement.RECEIPT else Voucher.PAYMENT,
        narration=f"{settlement.settlement_type.title()} - {settlement.party}",
        ref_model="Settlement",
        ref_id=settlement.id,
    )

    amt = settlement.amount

    # RECEIPT: Cash/Bank DR, AR CR
    if settlement.settlement_type == Settlement.RECEIPT:
        VoucherLine.objects.create(voucher=v, account=money, debit=amt)
        VoucherLine.objects.create(voucher=v, account=ar, credit=amt)

    # PAYMENT: AP DR, Cash/Bank CR
    else:
        VoucherLine.objects.create(voucher=v, account=ap, debit=amt)
        VoucherLine.objects.create(voucher=v, account=money, credit=amt)

    v.clean()
    return v

@transaction.atomic
def post_settlement(settlement: Settlement):
    cash = get_account("Cash")
    bank = get_account("Bank")
    ar = get_account("Accounts Receivable")
    ap = get_account("Accounts Payable")

    money = cash if settlement.payment_method == Settlement.CASH else bank

    voucher = Voucher.objects.create(
        date=settlement.date,
        voucher_type=(
            Voucher.RECEIPT
            if settlement.settlement_type == Settlement.RECEIPT
            else Voucher.PAYMENT
        ),
        narration=f"{settlement.settlement_type.title()} - {settlement.party}",
        ref_model="Settlement",
        ref_id=settlement.id,
    )

    amount = settlement.amount

    # ------------------------
    # RECEIPT (Customer paid)
    # ------------------------
    if settlement.settlement_type == Settlement.RECEIPT:
        # Accounting
        VoucherLine.objects.create(voucher=voucher, account=money, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=ar, credit=amount)

        # Party Ledger (Step C)
        post_party_ledger(
            voucher=voucher,
            party=settlement.party,
            debit=0,
            credit=amount
        )

    # ------------------------
    # PAYMENT (Supplier paid)
    # ------------------------
    else:
        # Accounting
        VoucherLine.objects.create(voucher=voucher, account=ap, debit=amount)
        VoucherLine.objects.create(voucher=voucher, account=money, credit=amount)

        # Party Ledger (Step C)
        post_party_ledger(
            voucher=voucher,
            party=settlement.party,
            debit=amount,
            credit=0
        )

    voucher.clean()

    settlement.voucher = voucher
    settlement.save(update_fields=["voucher"])

    return voucher