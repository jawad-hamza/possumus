from django.db import transaction
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine, Loans, LoanTransactions


def get_account(title: str) -> Account:
    return Account.objects.get(title=title, is_active=True)


@transaction.atomic
def post_loan_transaction(tx: LoanTransactions) -> Voucher:
    """
    Create accounting voucher for a loan transaction (Qard Hasan).
    """

    # Core accounts
    cash = get_account("Cash")
    bank = get_account("Bank")
    loan_payable = get_account("Loan Payable")
    loan_receivable = get_account("Loan Receivable")

    money_account = cash if tx.payment_method == LoanTransactions.CASH else bank

    # Decide which loan account to use
    loan_account = (
        loan_payable if tx.loan.loan_type == Loans.TAKEN
        else loan_receivable
    )

    voucher = Voucher.objects.create(
        date=tx.date or timezone.now().date(),
        voucher_type=Voucher.LOAN,
        narration=f"Loan {tx.tx_type} - {tx.loan.party}",
        ref_model="LoanTransaction",
        ref_id=tx.id,
    )

    amount = tx.amount
    lines = []

    # -----------------------------
    # LOAN TAKEN
    # -----------------------------
    if tx.loan.loan_type == Loans.TAKEN:
        if tx.tx_type == LoanTransactions.DISBURSE:
            # Cash/Bank DR | Loan Payable CR
            lines.append(VoucherLine(
                voucher=voucher,
                account=money_account,
                debit=amount
            ))
            lines.append(VoucherLine(
                voucher=voucher,
                account=loan_account,
                credit=amount
            ))
        else:
            # Loan Payable DR | Cash/Bank CR
            lines.append(VoucherLine(
                voucher=voucher,
                account=loan_account,
                debit=amount
            ))
            lines.append(VoucherLine(
                voucher=voucher,
                account=money_account,
                credit=amount
            ))

    # -----------------------------
    # LOAN GIVEN
    # -----------------------------
    else:
        if tx.tx_type == LoanTransactions.DISBURSE:
            # Loan Receivable DR | Cash/Bank CR
            lines.append(VoucherLine(
                voucher=voucher,
                account=loan_account,
                debit=amount
            ))
            lines.append(VoucherLine(
                voucher=voucher,
                account=money_account,
                credit=amount
            ))
        else:
            # Cash/Bank DR | Loan Receivable CR
            lines.append(VoucherLine(
                voucher=voucher,
                account=money_account,
                debit=amount
            ))
            lines.append(VoucherLine(
                voucher=voucher,
                account=loan_account,
                credit=amount
            ))

    VoucherLine.objects.bulk_create(lines)

    # Safety check
    voucher.clean()

    return voucher
