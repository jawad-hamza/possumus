from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine, LoanTransactions, Loans


def get_account(title: str) -> Account:
    return Account.objects.get(title=title, is_active=True)


@transaction.atomic
def post_loan_transaction(tx: LoanTransactions) -> Voucher:
    """
    Creates a balanced voucher for a loan transaction (interest-free).
    """
    cash = get_account("Cash")
    bank = get_account("Bank")
    loan_payable = get_account("Loan Payable (Qard Hasan)")
    loan_receivable = get_account("Loan Receivable (Qard Hasan)")

    money_account = cash if tx.payment_method == LoanTransactions.CASH else bank

    # Decide which loan account to use based on loan_type
    loan_account = loan_payable if tx.loan.loan_type == Loans.TAKEN else loan_receivable

    v = Voucher.objects.create(
        date=tx.date or timezone.localdate(),
        voucher_type=Voucher.LOAN,
        narration=f"Loan {tx.tx_type} ({tx.loan.party_name})",
        ref_model="LoanTransaction",
        ref_id=tx.id,
    )

    amt = Decimal(tx.amount)

    lines = []

    # TAKEN: we receive money -> Cash/Bank DR, Loan Payable CR (disburse)
    # TAKEN repayment: Loan Payable DR, Cash/Bank CR (repay)
    if tx.loan.loan_type == Loans.TAKEN:
        if tx.tx_type == LoanTransactions.DISBURSE:
            lines.append(VoucherLine(voucher=v, account=money_account, debit=amt, credit=0))
            lines.append(VoucherLine(voucher=v, account=loan_account, debit=0, credit=amt))
        else:  # REPAY
            lines.append(VoucherLine(voucher=v, account=loan_account, debit=amt, credit=0))
            lines.append(VoucherLine(voucher=v, account=money_account, debit=0, credit=amt))

    # GIVEN: we give money -> Loan Receivable DR, Cash/Bank CR (disburse)
    # GIVEN repayment: Cash/Bank DR, Loan Receivable CR (repay)
    else:
        if tx.tx_type == LoanTransactions.DISBURSE:
            lines.append(VoucherLine(voucher=v, account=loan_account, debit=amt, credit=0))
            lines.append(VoucherLine(voucher=v, account=money_account, debit=0, credit=amt))
        else:  # REPAY
            lines.append(VoucherLine(voucher=v, account=money_account, debit=amt, credit=0))
            lines.append(VoucherLine(voucher=v, account=loan_account, debit=0, credit=amt))

    VoucherLine.objects.bulk_create(lines)

    # Optional: enforce balance check (will raise if not balanced)
    v.clean()

    return v
