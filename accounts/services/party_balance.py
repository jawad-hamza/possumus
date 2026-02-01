from decimal import Decimal
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.contrib.contenttypes.models import ContentType

from accounts.models import PartyLedgerEntry


def get_party_balance(party) -> Decimal:
    """
    Returns NET balance for a party.
    +ve  => party owes us (receivable)
    -ve  => we owe party (payable)
    """

    ct = ContentType.objects.get_for_model(party)

    totals = PartyLedgerEntry.objects.filter(
        content_type=ct,
        object_id=party.id
    ).aggregate(
        debit=Coalesce(Sum("debit"), Decimal("0")),
        credit=Coalesce(Sum("credit"), Decimal("0")),
    )

    return totals["debit"] - totals["credit"]