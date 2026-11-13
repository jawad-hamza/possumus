"""Stock returns without an invoice: market returns from customers and returns to suppliers,
good stock and damaged stock, with their vouchers."""
from decimal import Decimal

from django.urls import reverse

from accounts.models import VoucherLine
from distribution.models import StockReturn, StockReturnItem
from distribution.services.common import DistributionError
from distribution.services.stock_returns import DAMAGED_STOCK, create_stock_return, damaged_held

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, DistributionCase


def line(product, quantity, unit, rate, condition=StockReturnItem.SALEABLE):
    return {
        "product": product,
        "quantity": Decimal(str(quantity)),
        "unit_level": unit,
        "unit_factor": product.pack_factor(unit),
        "rate": Decimal(str(rate)),
        "condition": condition,
    }


class StockReturnCase(DistributionCase):
    def voucher_map(self, stock_return):
        rows = {}
        for entry in VoucherLine.objects.filter(voucher=stock_return.voucher).select_related("account"):
            rows[entry.account.title] = rows.get(entry.account.title, Decimal("0")) + entry.debit - entry.credit
        return rows

    def market_return(self, *lines, settlement=StockReturn.ON_ACCOUNT, customer=None):
        return create_stock_return(
            kind=StockReturn.FROM_CUSTOMER, lines=list(lines), reason="Expired on the shelf",
            settlement=settlement, customer=customer if customer is not None else self.shop, user=self.admin_user,
        )


class MarketReturnTests(StockReturnCase):
    def test_good_stock_goes_back_on_the_shelf_and_the_customer_is_credited(self):
        stock_return = self.market_return(line(self.soap, 2, PACK_BOX, 144))

        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("624"))
        self.assertEqual((stock_return.total_amount, stock_return.cost_amount), (Decimal("288.00"), Decimal("240.00")))
        self.assertEqual(self.balance(self.shop), Decimal("-288.00"))
        self.assertEqual(
            self.voucher_map(stock_return),
            {"Sales Returns": Decimal("288.00"), "Accounts Receivable": Decimal("-288.00"), "Inventory": Decimal("240.00"), "Cost of Goods Sold": Decimal("-240.00")},
        )
        self.assert_books_balance()

    def test_damaged_stock_is_kept_out_of_saleable_stock_and_held_for_a_claim(self):
        stock_return = self.market_return(
            line(self.soap, 1, PACK_CARTON, 1400, StockReturnItem.DAMAGED), line(self.biscuit, 4, PACK_PIECE, 30)
        )

        self.refresh(self.soap, self.biscuit)
        self.assertEqual(self.soap.stock, Decimal("600"))
        self.assertEqual(self.biscuit.stock, Decimal("54"))
        self.assertEqual(
            self.voucher_map(stock_return),
            {
                "Sales Returns": Decimal("1520.00"),
                "Accounts Receivable": Decimal("-1520.00"),
                "Inventory": Decimal("80.00"),
                DAMAGED_STOCK: Decimal("1200.00"),
                "Cost of Goods Sold": Decimal("-1280.00"),
            },
        )
        self.assertEqual(damaged_held()[self.soap.pk], (Decimal("120.000"), Decimal("1200.00")))
        self.assert_books_balance()

    def test_cash_refund_needs_no_customer_and_on_account_needs_one(self):
        cash = self.market_return(line(self.biscuit, 1, PACK_PIECE, 30), settlement=StockReturn.CASH, customer=None)
        self.assertEqual(self.voucher_map(cash)["Cash"], Decimal("-30.00"))
        with self.assertRaisesMessage(DistributionError, "Choose the customer"):
            create_stock_return(kind=StockReturn.FROM_CUSTOMER, lines=[line(self.biscuit, 1, PACK_PIECE, 30)], reason="x")

    def test_market_return_is_entered_on_its_screen(self):
        page = self.client.get(reverse("distribution_return_add", args=["customer"]))
        self.assertEqual(page.status_code, 200)
        response = self.client.post(
            reverse("distribution_return_add", args=["customer"]),
            {
                "customer": self.shop.id, "return_date": self.today.isoformat(), "settlement": "account",
                "reason": "Leaking packs", "line_product": [self.soap.id], "line_quantity": ["5"],
                "line_unit": [PACK_PIECE], "line_rate": ["12"], "line_condition": ["damaged"],
            },
        )
        stock_return = StockReturn.objects.get()
        self.assertRedirects(response, reverse("distribution_return_detail", args=[stock_return.id]), fetch_redirect_response=False)
        detail = self.client.get(reverse("distribution_return_detail", args=[stock_return.id]))
        self.assertContains(detail, DAMAGED_STOCK)
        self.assertContains(self.client.get(reverse("distribution_return_list")), stock_return.display_number)


class SupplierReturnTests(StockReturnCase):
    def supplier_return(self, *lines, settlement=StockReturn.ON_ACCOUNT):
        return create_stock_return(
            kind=StockReturn.TO_SUPPLIER, lines=list(lines), reason="Claim", settlement=settlement,
            supplier=self.supplier, user=self.admin_user,
        )

    def test_good_stock_leaves_the_shelf_and_the_supplier_is_charged(self):
        # Charged 1,250 a carton; it stands in stock at 1,200: 50 is a gain.
        stock_return = self.supplier_return(line(self.soap, 1, PACK_CARTON, 1250))

        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("480"))
        self.assertEqual(
            self.voucher_map(stock_return),
            {"Accounts Payable": Decimal("1250.00"), "Inventory": Decimal("-1200.00"), "Inventory Adjustment Gain": Decimal("-50.00")},
        )
        self.assertEqual(self.balance(self.supplier), Decimal("1250.00"))
        self.assert_books_balance()

    def test_more_than_the_shelf_holds_is_refused(self):
        with self.assertRaisesMessage(DistributionError, "in stock to send back"):
            self.supplier_return(line(self.biscuit, 51, PACK_PIECE, 20))
        self.assertFalse(StockReturn.objects.exists())

    def test_damaged_stock_is_claimed_back_from_what_is_held(self):
        self.market_return(line(self.soap, 2, PACK_BOX, 144, StockReturnItem.DAMAGED))  # 24 pcs at cost 240
        with self.assertRaisesMessage(DistributionError, "held for a claim"):
            self.supplier_return(line(self.soap, 3, PACK_BOX, 120, StockReturnItem.DAMAGED))

        claim = self.supplier_return(line(self.soap, 2, PACK_BOX, 100, StockReturnItem.DAMAGED), settlement=StockReturn.CASH)

        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, Decimal("600"))  # saleable stock never moved
        self.assertEqual(
            self.voucher_map(claim),
            {"Cash": Decimal("200.00"), DAMAGED_STOCK: Decimal("-240.00"), "Inventory Adjustment Loss": Decimal("40.00")},
        )
        self.assertEqual(damaged_held()[self.soap.pk], (Decimal("0.000"), Decimal("0.00")))
        self.assertEqual(self.account_balance(DAMAGED_STOCK), Decimal("0.00"))
        self.assert_books_balance()

    def test_supplier_return_screen_offers_cost_rates(self):
        page = self.client.get(reverse("distribution_return_add", args=["supplier"]))
        self.assertEqual(page.status_code, 200)
        data = page.context["product_data"][str(self.soap.pk)]
        carton = next(level for level in data["levels"] if level["code"] == PACK_CARTON)
        self.assertEqual(carton["price"], "1200.00")
