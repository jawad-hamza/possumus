from datetime import date

from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import BankAccount
from accounts.services.party_statement import get_party_statement, get_unified_party_statement
from accounts.services.settlements import create_payment, create_receipt
from api.serializers import PartyStatementSerializer, SettlementCreateSerializer
from pos.models import Customer, Supplier
from pos.services.access import is_admin


class IsAdminRole(BasePermission):
    message = "Admin access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and is_admin(request.user))


class PartyStatementAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        customer_id = request.query_params.get("customer_id")
        supplier_id = request.query_params.get("supplier_id")
        from_date = request.query_params.get("from")
        to_date = request.query_params.get("to")

        customer = Customer.objects.filter(id=customer_id).first() if customer_id else None
        supplier = Supplier.objects.filter(id=supplier_id).first() if supplier_id else None

        if not customer and not supplier:
            return Response({"error": "customer_id or supplier_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from_date = date.fromisoformat(from_date) if from_date else None
            to_date = date.fromisoformat(to_date) if to_date else None
        except ValueError:
            return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        if customer and supplier:
            data = get_unified_party_statement(customer=customer, supplier=supplier, from_date=from_date, to_date=to_date)
        else:
            data = get_party_statement(party=customer or supplier, from_date=from_date, to_date=to_date)

        serializer = PartyStatementSerializer(data)
        return Response(serializer.data)


class SettlementAPIView(APIView):
    permission_classes = [IsAuthenticated, IsAdminRole]

    def post(self, request):
        serializer = SettlementCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        customer_id = payload.get("customer_id")
        supplier_id = payload.get("supplier_id")
        amount = payload["amount"]
        payment_method = payload["payment_method"]
        bank_account = None

        bank_account_id = payload.get("bank_account_id")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(id=bank_account_id, is_active=True).first()
            if bank_account is None:
                return Response({"error": "Bank ledger not found"}, status=status.HTTP_404_NOT_FOUND)

        if customer_id:
            customer = Customer.objects.filter(id=customer_id).first()
            if customer is None:
                return Response({"error": "Customer not found"}, status=status.HTTP_404_NOT_FOUND)
            voucher = create_receipt(party=customer, amount=amount, payment_method=payment_method, bank_account=bank_account)
        else:
            supplier = Supplier.objects.filter(id=supplier_id).first()
            if supplier is None:
                return Response({"error": "Supplier not found"}, status=status.HTTP_404_NOT_FOUND)
            voucher = create_payment(party=supplier, amount=amount, payment_method=payment_method, bank_account=bank_account)

        return Response({"voucher_id": voucher.id, "status": "ok"}, status=status.HTTP_201_CREATED)

