from decimal import Decimal, InvalidOperation

from django.db import transaction

from accounts.models import Account, CapitalEntry, Expense, ExpensePayment, MoneyTransfer, Voucher, VoucherLine
from accounts.services.cash_bank import resolve_money_account


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount") from exc


def _ensure_account(title: str, acc_type: str) -> Account:
    account, _ = Account.objects.get_or_create(title=title, defaults={"type": acc_type})
    if account.type != acc_type:
        raise ValueError(f"Account '{title}' exists with the wrong type")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def _owner_capital_account() -> Account:
    return _ensure_account("Owner Capital", Account.EQUITY)


def _money_label(method: str, bank_account=None, bank_name="", bank_account_number="") -> str:
    if method == Expense.CASH:
        return "Cash"
    if bank_account is not None:
        return bank_account.display_name
    if (bank_name or "").strip() and (bank_account_number or "").strip():
        return f"{bank_name.strip()} ({bank_account_number.strip()})"
    if (bank_name or "").strip():
        return bank_name.strip()
    return "Bank"


def _sync_voucher(*, voucher_type: str, ref_model: str, ref_id: int, narration: str, date):
    voucher, created = Voucher.objects.get_or_create(
        voucher_type=voucher_type,
        ref_model=ref_model,
        ref_id=ref_id,
        defaults={"narration": narration, "date": date},
    )
    if not created:
        voucher.narration = narration
        voucher.date = date
        voucher.save(update_fields=["narration", "date"])
        voucher.lines.all().delete()
    return voucher


@transaction.atomic
def post_expense_payment(payment_id: int) -> Voucher:
    payment = ExpensePayment.objects.select_related("expense", "bank_account").select_for_update().get(pk=payment_id)
    expense = Expense.objects.select_for_update().get(pk=payment.expense_id)
    amount = _to_decimal(payment.amount)
    already_posted = payment.voucher_id is not None

    if amount <= 0:
        raise ValueError("Payment amount must be greater than zero")
    if not already_posted and amount > expense.balance_due:
        raise ValueError("Payment amount cannot exceed the remaining expense due")

    source_account = resolve_money_account(payment.payment_method, payment.bank_account)
    payable_account = _ensure_account("Expense Payable", Account.LIABILITY)
    source_label = _money_label(payment.payment_method, payment.bank_account, payment.bank_name, payment.bank_account_number)

    voucher = _sync_voucher(
        voucher_type=Voucher.PAYMENT,
        ref_model="ExpensePayment",
        ref_id=payment.id,
        narration=f"Expense payment #{payment.id} - {expense.display_title} from {source_label}",
        date=payment.date,
    )
    VoucherLine.objects.create(voucher=voucher, account=payable_account, debit=amount)
    VoucherLine.objects.create(voucher=voucher, account=source_account, credit=amount)
    voucher.clean()

    if payment.voucher_id != voucher.id:
        payment.voucher = voucher
        payment.save(update_fields=["voucher"])

    if not already_posted:
        expense.paid_amount = (expense.paid_amount or Decimal("0")) + amount
        expense.full_clean()
        expense.save(update_fields=["paid_amount"])

    return voucher


@transaction.atomic
def create_expense_payment(**kwargs) -> ExpensePayment:
    payment = ExpensePayment.objects.create(**kwargs)
    post_expense_payment(payment.id)
    payment.refresh_from_db()
    return payment


@transaction.atomic
def post_capital_entry(entry_id: int) -> Voucher:
    entry = CapitalEntry.objects.select_related("bank_account").select_for_update().get(pk=entry_id)
    amount = _to_decimal(entry.amount)
    if amount <= 0:
        raise ValueError("Amount must be greater than zero")

    destination_account = resolve_money_account(entry.deposit_to, entry.bank_account)
    owner_capital_account = _owner_capital_account()
    destination_label = _money_label(entry.deposit_to, entry.bank_account, entry.bank_name, entry.bank_account_number)

    voucher = _sync_voucher(
        voucher_type=Voucher.JOURNAL,
        ref_model="CapitalEntry",
        ref_id=entry.id,
        narration=f"Funds introduced #{entry.id} - {entry.source_name} into {destination_label}",
        date=entry.date,
    )
    VoucherLine.objects.create(voucher=voucher, account=destination_account, debit=amount)
    VoucherLine.objects.create(voucher=voucher, account=owner_capital_account, credit=amount)
    voucher.clean()

    if entry.voucher_id != voucher.id:
        entry.voucher = voucher
        entry.save(update_fields=["voucher"])

    return voucher


@transaction.atomic
def create_capital_entry(**kwargs) -> CapitalEntry:
    entry = CapitalEntry.objects.create(**kwargs)
    post_capital_entry(entry.id)
    entry.refresh_from_db()
    return entry


@transaction.atomic
def post_money_transfer(transfer_id: int) -> Voucher:
    transfer = MoneyTransfer.objects.select_related("from_bank_account", "to_bank_account").select_for_update().get(pk=transfer_id)
    amount = _to_decimal(transfer.amount)
    if amount <= 0:
        raise ValueError("Transfer amount must be greater than zero")
    if transfer.from_account == transfer.to_account and transfer.from_bank_account_id == transfer.to_bank_account_id:
        raise ValueError("Transfer in and transfer out accounts must be different")

    source_account = resolve_money_account(transfer.from_account, transfer.from_bank_account)
    destination_account = resolve_money_account(transfer.to_account, transfer.to_bank_account)
    from_label = _money_label(transfer.from_account, transfer.from_bank_account, transfer.from_bank_name, transfer.from_bank_account_number)
    to_label = _money_label(transfer.to_account, transfer.to_bank_account, transfer.to_bank_name, transfer.to_bank_account_number)

    voucher = _sync_voucher(
        voucher_type=Voucher.JOURNAL,
        ref_model="MoneyTransfer",
        ref_id=transfer.id,
        narration=f"Transfer #{transfer.id} - out from {from_label} into {to_label}",
        date=transfer.date,
    )
    VoucherLine.objects.create(voucher=voucher, account=destination_account, debit=amount)
    VoucherLine.objects.create(voucher=voucher, account=source_account, credit=amount)
    voucher.clean()

    if transfer.voucher_id != voucher.id:
        transfer.voucher = voucher
        transfer.save(update_fields=["voucher"])

    return voucher


@transaction.atomic
def create_money_transfer(**kwargs) -> MoneyTransfer:
    transfer = MoneyTransfer.objects.create(**kwargs)
    post_money_transfer(transfer.id)
    transfer.refresh_from_db()
    return transfer

