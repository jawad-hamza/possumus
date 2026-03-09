from rest_framework import serializers

from accounts.models import Settlement


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


class SettlementCreateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=False)
    supplier_id = serializers.IntegerField(required=False)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    payment_method = serializers.ChoiceField(choices=Settlement.PAYMENT_METHOD_CHOICES)
    bank_account_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        customer_id = attrs.get("customer_id")
        supplier_id = attrs.get("supplier_id")
        amount = attrs.get("amount")
        payment_method = attrs.get("payment_method")
        bank_account_id = attrs.get("bank_account_id")

        if bool(customer_id) == bool(supplier_id):
            raise serializers.ValidationError("Provide exactly one of customer_id or supplier_id.")
        if amount is None or amount <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")

        return attrs
