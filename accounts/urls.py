from django.urls import path
from . import views


urlpatterns = [
    path("", views.accounts_home, name="accounts_home"),
    path("money-breakdown/", views.money_breakdown, name="money_breakdown"),
    path("money/", views.money_desk, name="money_desk"),
    path("expenses/", views.expense_list, name="expense_list"),
    path("trial-balance/", views.trial_balance_view, name="trial_balance"),
    path("profit-loss/", views.profit_loss_view, name="profit_loss"),
    path("balance-sheet/", views.balance_sheet_view, name="balance_sheet"),
    path("ledger/<int:account_id>/", views.account_ledger, name="account_ledger"),
    path("party/<str:model>/<int:pk>/", views.party_statement, name="party_statement"),
]


