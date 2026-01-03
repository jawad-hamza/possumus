from accounts.models import PartyLedgerEntry
from django.contrib.contenttypes.models import ContentType


def post_party_ledger(voucher, party, debit=0, credit=0):
    """
    Create party sub-ledger entry linked to a voucher.
    """
    PartyLedgerEntry.objects.create(
        voucher=voucher,
        content_type=ContentType.objects.get_for_model(party),
        object_id=party.id,
        debit=debit,
        credit=credit,
    )
