from django.db import transaction
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import BankAccount, Settlement
from accounts.services.audit import record_audit
from accounts.services.bank_receipts import UPLOAD_FIELD, attach_receipt, mark_statement_rows, request_upload
from accounts.services.party_statement import get_party_statement, get_unified_party_statement
from accounts.services.settlements import create_payment, create_receipt
from api.serializers import PartyStatementQuerySerializer, PartyStatementSerializer, SettlementCreateSerializer
from pos.models import Customer, Supplier
from pos.services.access import ACCESS_MANAGE, ACCESS_VIEW, user_can_access_module, user_requires_business_setup

try:  # The licence/billing lock helper is newer than this module; fall back to the billing check.
    from pos.services.access import workspace_is_locked
except ImportError:  # pragma: no cover - depends on the deployed access module
    from pos.services.access import workspace_requires_billing as workspace_is_locked


# Session cookies only: the API is used by the app's own pages, and Basic auth would
# hand attackers a password-guessing endpoint that skips the login lockout.
API_AUTHENTICATION_CLASSES = [SessionAuthentication]


class WorkspaceIsOpen(BasePermission):
    """The same gate the HTML views apply: no API access while setup or billing/licence blocks the workspace."""

    message = "This workspace is locked. Open the app to finish setup or renew access."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user_requires_business_setup(user):
            return False
        return not workspace_is_locked(user)


class HasPartiesViewAccess(BasePermission):
    message = "Party access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and user_can_access_module(request.user, "parties", ACCESS_VIEW))


class HasPartiesManageAccess(BasePermission):
    message = "Party manage access required."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and user_can_access_module(request.user, "parties", ACCESS_MANAGE))


class PartyStatementAPIView(APIView):
    authentication_classes = API_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated, WorkspaceIsOpen, HasPartiesViewAccess]

    def get(self, request):
        query = PartyStatementQuerySerializer(data=request.query_params)
        if not query.is_valid():
            if "from" in query.errors or "to" in query.errors:
                return Response({"error": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
            return Response({"error": "Invalid query parameters.", "details": query.errors}, status=status.HTTP_400_BAD_REQUEST)
        params = query.validated_data

        customer_id = params.get("customer_id")
        supplier_id = params.get("supplier_id")
        customer = Customer.objects.filter(id=customer_id).first() if customer_id else None
        supplier = Supplier.objects.filter(id=supplier_id).first() if supplier_id else None

        if not customer and not supplier:
            return Response({"error": "customer_id or supplier_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        from_date = params.get("from")
        to_date = params.get("to")
        if customer and supplier:
            data = get_unified_party_statement(customer=customer, supplier=supplier, from_date=from_date, to_date=to_date)
        else:
            data = get_party_statement(party=customer or supplier, from_date=from_date, to_date=to_date)

        payload = dict(PartyStatementSerializer(data).data)
        limit = params.get("limit")
        if limit is not None:
            offset = params.get("offset", 0)
            rows = payload["rows"]
            payload["total_rows"] = len(rows)
            payload["limit"] = limit
            payload["offset"] = offset
            payload["rows"] = rows[offset : offset + limit]
        # Each row says whether its bank payment carries a receipt, and where that receipt is shown.
        mark_statement_rows(payload["rows"], request.user)
        return Response(payload)


class SettlementAPIView(APIView):
    authentication_classes = API_AUTHENTICATION_CLASSES
    permission_classes = [IsAuthenticated, WorkspaceIsOpen, HasPartiesManageAccess]

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

        # A bank settlement carries its receipt (sent as multipart); a cash one carries none.
        bank_receipt, receipt_problem = request_upload(request, bank_payment=payment_method == Settlement.BANK)
        if receipt_problem:
            return Response({UPLOAD_FIELD: [receipt_problem]}, status=status.HTTP_400_BAD_REQUEST)

        # The money posting and its audit trail succeed or fail together.
        try:
            with transaction.atomic():
                if customer_id:
                    party = Customer.objects.filter(id=customer_id).first()
                    if party is None:
                        return Response({"error": "Customer not found"}, status=status.HTTP_404_NOT_FOUND)
                    voucher = create_receipt(party=party, amount=amount, payment_method=payment_method, bank_account=bank_account)
                    action, verb = "settlement_receipt_created", "Receipt from"
                else:
                    party = Supplier.objects.filter(id=supplier_id).first()
                    if party is None:
                        return Response({"error": "Supplier not found"}, status=status.HTTP_404_NOT_FOUND)
                    voucher = create_payment(party=party, amount=amount, payment_method=payment_method, bank_account=bank_account)
                    action, verb = "settlement_payment_created", "Payment to"

                if bank_receipt:
                    attach_receipt(Settlement.objects.get(pk=voucher.ref_id), bank_receipt, actor=request.user, audit=False)
                record_audit(
                    action=action,
                    description=f"{verb} {party} recorded through the API.",
                    actor=request.user,
                    target=voucher,
                    metadata={
                        "source": "api",
                        "party_type": party.__class__.__name__,
                        "party_id": party.id,
                        "amount": str(amount),
                        "payment_method": payment_method,
                        "bank_account_id": bank_account.id if bank_account else None,
                    },
                )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"voucher_id": voucher.id, "status": "ok"}, status=status.HTTP_201_CREATED)
