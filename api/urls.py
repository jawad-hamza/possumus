from django.urls import path
from api.views import PartyStatementAPIView

urlpatterns = [
    path("party-statement/", PartyStatementAPIView.as_view()),
]