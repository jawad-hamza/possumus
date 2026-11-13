from rest_framework import serializers

from accounts.models import Settlement

MAX_STATEMENT_PAGE_SIZE = 500


class PartyStatementQuerySerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=False, min_value=1)
    supplier_id = serializers.IntegerField(required=False, min_value=1)
    # Paging is opt-in: without "limit" the full statement is returned as before.
    limit = serializers.IntegerField(required=False, min_value=1, max_value=MAX_STATEMENT_PAGE_SIZE)
    offset = serializers.IntegerField(required=False, min_value=0, max_value=10_000_000)

    def get_fields(self):
        fields = super().get_fields()
        # "from" is a Python keyword, so these two cannot be declared as class attributes.
        fields["from"] = serializers.DateField(required=False)
        fields["to"] = serializers.DateField(required=False)
        return fields

    def validate(self, attrs):
        if attrs.get("offset") and attrs.get("limit") is None:
            raise serializers.ValidationError({"offset": "offset needs a limit."})
        from_date, to_date = attrs.get("from"), attrs.get("to")
        if from_date and to_date and from_date > to_date:
            raise serializers.ValidationError({"to": "The end date cannot be before the start date."})
        return attrs


class PartyStatementRowSerializer(serializers.Serializer):
    date = serializers.DateField()
    voucher_id = serializers.IntegerField()
    voucher_type = serializers.CharField()
    ref = serializers.CharField()
    document = serializers.CharField(required=False, allow_blank=True)
    # Goods on the entry (an invoice's total quantity, a return's as a minus); null when it moved none.
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, required=False, allow_null=True)
    debit = serializers.DecimalField(max_digits=12, decimal_places=2)
    credit = serializers.DecimalField(max_digits=12, decimal_places=2)
    running_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    balance_type = serializers.CharField()
    display_balance = serializers.DecimalField(max_digits=12, decimal_places=2)


class PartyStatementSerializer(serializers.Serializer):
    party = serializers.CharField()
    from_date = serializers.DateField(required=False, allow_null=True)
    to_date = serializers.DateField(required=False, allow_null=True)
    opening_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    closing_balance = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_quantity = serializers.DecimalField(max_digits=15, decimal_places=3, required=False)
    balance_type = serializers.CharField()
    rows = PartyStatementRowSerializer(many=True)


class SettlementCreateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField(required=False, min_value=1)
    supplier_id = serializers.IntegerField(required=False, min_value=1)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    payment_method = serializers.ChoiceField(choices=Settlement.PAYMENT_METHOD_CHOICES)
    bank_account_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate(self, attrs):
        customer_id = attrs.get("customer_id")
        supplier_id = attrs.get("supplier_id")
        amount = attrs.get("amount")

        if bool(customer_id) == bool(supplier_id):
            raise serializers.ValidationError("Provide exactly one of customer_id or supplier_id.")
        if amount is None or amount <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        # A bank ledger on a cash settlement would post to Cash while the record points
        # at a bank, so the two fields must agree. Bank without a ledger is allowed and
        # posts to the generic Bank account, exactly like the settlement form.
        if attrs.get("payment_method") == Settlement.CASH and attrs.get("bank_account_id"):
            raise serializers.ValidationError({"bank_account_id": "A bank ledger can only be used with the bank payment method."})

        return attrs
