from django.shortcuts import render

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from pos.models import Customer, Supplier
from accounts.services.party_statement import (
    get_party_statement,
    get_unified_party_statement,
)
from api.serializers import PartyStatementSerializer
from datetime import date


class PartyStatementAPIView(APIView):
    """
    GET /api/party-statement/?customer_id=&supplier_id=&from=&to=
    """

    def get(self, request):
        customer_id = request.query_params.get("customer_id")
        supplier_id = request.query_params.get("supplier_id")
        from_date = request.query_params.get("from")
        to_date = request.query_params.get("to")

        customer = None
        supplier = None

        if customer_id:
            customer = Customer.objects.filter(id=customer_id).first()

        if supplier_id:
            supplier = Supplier.objects.filter(id=supplier_id).first()

        if not customer and not supplier:
            return Response(
                {"error": "customer_id or supplier_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # parse dates
        from_date = date.fromisoformat(from_date) if from_date else None
        to_date = date.fromisoformat(to_date) if to_date else None

        if customer and supplier:
            data = get_unified_party_statement(
                customer=customer,
                supplier=supplier
            )
        else:
            party = customer or supplier
            data = get_party_statement(
                party=party,
                from_date=from_date,
                to_date=to_date
            )

        serializer = PartyStatementSerializer(data)
        return Response(serializer.data)