from rest_framework import serializers


class PartyStatementRowSerializer(serializers.Serializer):
    date = serializers.DateField()
    voucher_id = serializers.IntegerField()
    voucher_type = serializers.CharField()
    ref = serializers.CharField()
    debit = serializers.DecimalField(max_digits=12, decimal_places=2)
    credit = serializers.DecimalField(max_digits=12, decimal_places=2)
    running_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    balance_type = serializers.CharField()
    display_balance = serializers.DecimalField(max_digits=12, decimal_places=2)


class PartyStatementSerializer(serializers.Serializer):
    party = serializers.CharField()
    opening_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    balance_type = serializers.CharField()
    rows = PartyStatementRowSerializer(many=True)