from django.urls import path

from api.views import PartyStatementAPIView, SettlementAPIView

urlpatterns = [
    path("party-statement/", PartyStatementAPIView.as_view(), name="party_statement_api"),
    path("settlements/", SettlementAPIView.as_view(), name="settlement_api"),
]
