"""A small distributor for the distribution tests: soap packed 10 boxes x 12 pieces a carton,
biscuits sold by the piece, one route with its order booker and a shop on credit."""
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from accounts.models import Account, Voucher, VoucherLine
from accounts.services.chart_of_accounts import ensure_default_accounts
from accounts.services.party_balance import get_party_balance
from pos.models import PACK_BOX, PACK_CARTON, PACK_PIECE, Customer, OrderBooker, Product, Route, Supplier, SystemSettings
from pos.test_support import AuthenticatedAdminTestCase

from distribution.models import DeliveryMan, SalesOrder, SalesOrderItem, Vehicle

ZERO = Decimal("0")


class DistributionCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        ensure_default_accounts()
        self.today = timezone.localdate()
        # Soap: 1 carton = 10 boxes = 120 pieces. Cost 10, price 12 a piece (1,440 a carton).
        self.soap = Product.objects.create(
            name="Soap", sku="SOAP", cost_price=Decimal("10"), price=Decimal("12"), stock=Decimal("600"),
            pieces_per_box=12, boxes_per_carton=10,
        )
        self.biscuit = Product.objects.create(
            name="Biscuit", sku="BIS", cost_price=Decimal("20"), price=Decimal("30"), stock=Decimal("50")
        )
        self.route = Route.objects.create(name="Main Bazaar", city="Mirpur")
        self.other_route = Route.objects.create(name="Canal Road", city="Mirpur")
        self.booker = OrderBooker.objects.create(name="Imran")
        self.booker.routes.add(self.route)
        self.shop = Customer.objects.create(name="Ali Store", route=self.route, order_booker=self.booker)
        self.other_shop = Customer.objects.create(name="Bilal Mart", route=self.other_route)
        self.supplier = Supplier.objects.create(name="Unilever Agency")
        self.driver = DeliveryMan.objects.create(name="Aslam")
        self.van = Vehicle.objects.create(name="Shehzore", number="LES-123")

    # ------------------------------------------------------------------ helpers
    def book(self, customer=None, *lines, booker=None, route=None):
        """lines: (product, quantity, unit[, rate[, discount]])."""
        customer = customer or self.shop
        order = SalesOrder.objects.create(
            customer=customer,
            route=route if route is not None else customer.route,
            order_booker=booker if booker is not None else customer.order_booker,
            created_by=self.admin_user,
        )
        for line in lines:
            product, quantity, unit = line[0], Decimal(str(line[1])), line[2]
            rate = Decimal(str(line[3])) if len(line) > 3 else product.pack_price(unit)
            discount = Decimal(str(line[4])) if len(line) > 4 else ZERO
            SalesOrderItem.objects.create(
                order=order, product=product, quantity=quantity, unit_level=unit,
                unit_factor=product.pack_factor(unit), price=rate, discount_percent=discount,
            )
        return order

    def refresh(self, *objects):
        for obj in objects:
            obj.refresh_from_db()

    def account_balance(self, title):
        """Debit less credit on one account."""
        totals = VoucherLine.objects.filter(account__title=title).aggregate(d=Sum("debit"), c=Sum("credit"))
        return (totals["d"] or ZERO) - (totals["c"] or ZERO)

    def assert_books_balance(self):
        totals = VoucherLine.objects.aggregate(d=Sum("debit"), c=Sum("credit"))
        self.assertEqual(totals["d"] or ZERO, totals["c"] or ZERO)
        for voucher in Voucher.objects.all():
            voucher.clean()

    def balance(self, party):
        return Decimal(get_party_balance(party))

    def settings_row(self, **values):
        row = SystemSettings.load_raw()
        for key, value in values.items():
            setattr(row, key, value)
        row.save()
        return row

    def messages_of(self, response):
        return [str(message) for message in response.context["messages"]]


__all__ = ["DistributionCase", "PACK_BOX", "PACK_CARTON", "PACK_PIECE", "Account", "ZERO"]
