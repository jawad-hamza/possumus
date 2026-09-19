from decimal import Decimal, InvalidOperation

from django.db import transaction

from accounts.models import Account, Expense, Voucher, VoucherLine
from accounts.services.books_lock import assert_books_open, normalize_voucher_date
from accounts.services.cash_bank import resolve_money_account


ZERO = Decimal("0.00")


def _to_decimal(value):
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid monetary value") from exc


def _expense_payable_account():
    account, _ = Account.objects.get_or_create(
        title="Expense Payable",
        defaults={"type": Account.LIABILITY},
    )
    if account.type != Account.LIABILITY:
        raise ValueError("Expense Payable account must be a liability account")
    if not account.is_active:
        account.is_active = True
        account.save(update_fields=["is_active"])
    return account


def _sync_voucher(*, ref_id, narration, date, actor=None):
    date = normalize_voucher_date(date)
    assert_books_open(date, action=f"post expense #{ref_id}")
    voucher, created = Voucher.objects.get_or_create(
        voucher_type=Voucher.EXPENSE,
        ref_model="Expense",
        ref_id=ref_id,
        defaults={
            "narration": narration,
            "date": date,
            "created_by": actor,
        },
    )

    if not created:
        # Saving first lets the books-lock guard refuse before any line is removed.
        voucher.narration = narration
        voucher.date = date
        voucher.save(update_fields=["narration", "date"])
        voucher.lines.all().delete()

    return voucher


@transaction.atomic
def post_expense(expense_id, *, actor=None):
    expense = Expense.objects.select_related("category", "category__account", "bank_account").get(pk=expense_id)
    amount = _to_decimal(expense.amount)
    paid_amount = _to_decimal(expense.paid_amount or ZERO)

    if amount <= 0:
        raise ValueError("Expense amount must be greater than zero")
    if paid_amount < 0:
        raise ValueError("Paid amount cannot be negative")
    if paid_amount > amount:
        raise ValueError("Paid amount cannot exceed expense amount")

    if expense.category.account_id is None:
        expense.category.save()
        expense.refresh_from_db(fields=[])

    expense_account = expense.category.account
    if expense_account.type != Account.EXPENSE:
        raise ValueError("Expense type must be linked to an expense account")

    balance_due = amount - paid_amount
    payment_label = "Cash" if expense.payment_method == Expense.CASH else (expense.bank_account.display_name if expense.bank_account_id else "Bank")
    voucher = _sync_voucher(
        ref_id=expense.id,
        narration=f"Expense #{expense.id} - {expense.display_title} via {payment_label}",
        date=expense.date,
        actor=actor,
    )

    VoucherLine.objects.create(
        voucher=voucher,
        account=expense_account,
        debit=amount,
    )

    if paid_amount > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=resolve_money_account(expense.payment_method, expense.bank_account),
            credit=paid_amount,
        )

    if balance_due > 0:
        VoucherLine.objects.create(
            voucher=voucher,
            account=_expense_payable_account(),
            credit=balance_due,
        )

    voucher.clean()

    if expense.voucher_id != voucher.id:
        expense.voucher = voucher
        expense.save(update_fields=["voucher"])

    return voucher
