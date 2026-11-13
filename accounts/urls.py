from django.urls import path

from . import receipt_views, report_views, views


urlpatterns = [
    path("", views.accounts_home, name="accounts_home"),
    path("money-breakdown/", views.money_breakdown, name="money_breakdown"),
    path("money/", views.money_desk, name="money_desk"),
    path("expenses/", views.expense_list, name="expense_list"),
    path("ajax/quick/expense-types/", views.quick_create_expense_type, name="quick_create_expense_type"),
    path("ajax/quick/bank-accounts/", views.quick_create_bank_account, name="quick_create_bank_account"),
    path("receipts/<slug:kind>/<int:pk>/", receipt_views.bank_receipt, name="bank_receipt"),
    path("receipts/<slug:kind>/<int:pk>/file/", receipt_views.bank_receipt_file, name="bank_receipt_file"),
    path("books-lock/", report_views.books_lock_update, name="books_lock_update"),
    path("trial-balance/", report_views.trial_balance_view, name="trial_balance"),
    path("profit-loss/", report_views.profit_loss_view, name="profit_loss"),
    path("balance-sheet/", report_views.balance_sheet_view, name="balance_sheet"),
    path("day-book/", report_views.day_book, name="day_book"),
    path("cash-flow/", report_views.cash_flow, name="cash_flow"),
    path("receivables-aging/", report_views.receivables_aging, name="receivables_aging"),
    path("payables-aging/", report_views.payables_aging, name="payables_aging"),
    path("expenses-by-type/", report_views.expense_by_category_report, name="expense_by_category"),
    path("ledger/<int:account_id>/", report_views.account_ledger, name="account_ledger"),
    path("party/<str:model>/<int:pk>/", report_views.party_statement, name="party_statement"),
    path("party/<str:model>/<int:pk>/opening-balance/", report_views.party_opening_balance, name="party_opening_balance"),
]
