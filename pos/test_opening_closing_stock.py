"""Opening and closing stock, daily and monthly, and the day-by-day view.

Soap is packed 10 boxes x 12 pieces a carton (120 pcs) at a cost of 10 a piece. Its story,
in pieces:

* five days before last month: opening stock 2 Ctn (240);
* day 1 of the story (the 3rd of last month): bought 3 Ctn (+360), sold 2 Box and 5 pcs
  (-29) with 1 Box free (-12), the customer brought 3 pcs back (+3);
* day 2 (the 10th): 1 Ctn back to the supplier (-120), a market return of 2 Box good (+24)
  and 1 Ctn damaged (held for a claim, no stock), 5 pcs good back to the company (-5),
  7 pcs written off and 3 pcs found (-4);
* today: sold 1 Box (-12) and bought 1 Ctn (+120), after the month being looked at.
"""
import csv
import io
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from distribution.models import StockReturn, StockReturnItem
from distribution.services.stock_returns import create_stock_return
from pos.models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_PIECE,
    Category,
    Customer,
    Product,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    Sale,
    SaleItem,
    SaleReturn,
    StockAdjustment,
    Supplier,
)
from pos.services import reporting
from pos.services.returns import create_purchase_return, create_sale_return
from pos.services.stock_control import create_stock_adjustment
from pos.test_support import AuthenticatedAdminTestCase

ZERO = Decimal("0")
MOVEMENT_KEYS = ("purchased", "sold", "returned_in", "returned_out", "adjusted")


def at_local(day, hour=12, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


def read_csv(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def qty(value):
    return Decimal(value or 0)


@override_settings(TIME_ZONE="Asia/Karachi")
class OpeningClosingStockCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.month_start = (self.today.replace(day=1) - timedelta(days=1)).replace(day=1)
        self.month_end = self.today.replace(day=1) - timedelta(days=1)
        self.day1 = self.month_start + timedelta(days=2)
        self.day2 = self.month_start + timedelta(days=9)

        self.category = Category.objects.create(name="Soaps")
        self.other_category = Category.objects.create(name="Snacks")
        self.soap = Product.objects.create(
            name="Soap", sku="SOAP", category=self.category, cost_price=Decimal("10"), price=Decimal("12"),
            stock=Decimal("0"), pieces_per_box=12, boxes_per_carton=10,
        )
        # Stock that was on hand before anything was recorded, and never moves.
        self.biscuit = Product.objects.create(
            name="Biscuit", sku="BIS", category=self.other_category, cost_price=Decimal("20"), price=Decimal("30"), stock=Decimal("50")
        )
        # Nothing on hand and nothing moved: hidden unless every product is asked for.
        self.idle = Product.objects.create(name="Idle", sku="IDLE", cost_price=Decimal("5"), price=Decimal("8"), stock=Decimal("0"))
        self.customer = Customer.objects.create(name="Ali Store")
        self.supplier = Supplier.objects.create(name="Unilever Agency")
        self.record_story()

    # ------------------------------------------------------------------ the story
    def record_story(self):
        opening = create_stock_adjustment(
            product=self.soap, adjustment_mode=StockAdjustment.OPENING_STOCK, reason="Opening",
            quantity_delta=Decimal("240"), actor=self.admin_user,
        )
        StockAdjustment.objects.filter(pk=opening.pk).update(created_at=at_local(self.month_start - timedelta(days=5)))

        # Day 1: bought in cartons, sold in boxes and pieces with a free box, 3 pcs back.
        self.purchase = Purchase.objects.create(payment_method=Purchase.CASH, supplier=self.supplier)
        self.purchase_item = PurchaseItem.objects.create(
            purchase=self.purchase, product=self.soap, quantity=Decimal("3"), unit_level=PACK_CARTON,
            cost_price=Decimal("1200"), sale_price=Decimal("1440"),
        )
        Purchase.objects.filter(pk=self.purchase.pk).update(created_at=at_local(self.day1, 9))

        sale = Sale.objects.create(payment_method=Sale.CASH, customer=self.customer, sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.soap, quantity=Decimal("2"), unit_level=PACK_BOX)
        pieces = SaleItem.objects.create(sale=sale, product=self.soap, quantity=Decimal("5"), unit_level=PACK_PIECE)
        SaleItem.objects.create(
            sale=sale, product=self.soap, quantity=Decimal("1"), unit_level=PACK_BOX, is_free=True,
            price=Decimal("0"), discount_percent=Decimal("0"),
        )
        sale.refresh_from_db()
        sale.paid_amount = sale.total_amount
        sale.save(update_fields=["paid_amount"])
        Sale.objects.filter(pk=sale.pk).update(created_at=at_local(self.day1, 11))
        sale_return = create_sale_return(
            sale=sale, quantities={pieces.id: "3"}, refund_method=SaleReturn.CASH, reason="Wrong item", actor=self.admin_user
        )
        SaleReturn.objects.filter(pk=sale_return.pk).update(created_at=at_local(self.day1, 17))

        # Day 2: returns both ways, distribution stock returns good and damaged, adjustments.
        purchase_return = create_purchase_return(
            purchase=self.purchase, quantities={self.purchase_item.id: "1"}, settlement_method=PurchaseReturn.PAYABLE,
            reason="Leaking", actor=self.admin_user,
        )
        PurchaseReturn.objects.filter(pk=purchase_return.pk).update(created_at=at_local(self.day2, 9))
        create_stock_return(
            kind=StockReturn.FROM_CUSTOMER, customer=self.customer, reason="Market return", return_date=self.day2,
            user=self.admin_user,
            lines=[
                self.return_line(2, PACK_BOX, 144),
                self.return_line(1, PACK_CARTON, 1400, StockReturnItem.DAMAGED),
            ],
        )
        create_stock_return(
            kind=StockReturn.TO_SUPPLIER, supplier=self.supplier, reason="Short expiry", return_date=self.day2,
            user=self.admin_user, lines=[self.return_line(5, PACK_PIECE, 10)],
        )
        for delta, minute in (("-7", 0), ("3", 30)):
            adjustment = create_stock_adjustment(
                product=self.soap, adjustment_mode=StockAdjustment.ADJUSTMENT, reason="Count",
                quantity_delta=Decimal(delta), actor=self.admin_user,
            )
            StockAdjustment.objects.filter(pk=adjustment.pk).update(created_at=at_local(self.day2, 18, minute))

        # Today, after the month being looked at.
        later = Sale.objects.create(payment_method=Sale.CASH, sold_by=self.admin_user)
        SaleItem.objects.create(sale=later, product=self.soap, quantity=Decimal("1"), unit_level=PACK_BOX)
        PurchaseItem.objects.create(
            purchase=Purchase.objects.create(payment_method=Purchase.CASH), product=self.soap, quantity=Decimal("1"),
            unit_level=PACK_CARTON, cost_price=Decimal("1200"), sale_price=Decimal("1440"),
        )
        self.soap.refresh_from_db()

    def return_line(self, quantity, unit, rate, condition=StockReturnItem.SALEABLE):
        return {
            "product": self.soap,
            "quantity": Decimal(str(quantity)),
            "unit_level": unit,
            "unit_factor": self.soap.pack_factor(unit),
            "rate": Decimal(str(rate)),
            "condition": condition,
        }

    # ------------------------------------------------------------------ helpers
    def rows_by_product(self, report):
        return {row["product"]: row for row in report.rows}

    def assert_rows_tie(self, report):
        """Opening + purchased - sold + returned in - returned out + adjusted = closing, every row."""
        for row in report.rows:
            moved = (
                qty(row["purchased"]) - qty(row["sold"]) + qty(row["returned_in"]) - qty(row["returned_out"]) + qty(row["adjusted"])
            )
            self.assertEqual(row["opening"] + moved, row["closing"], row["product"])

    def movements(self, row):
        return tuple(qty(row[key]) for key in MOVEMENT_KEYS)


class OpeningClosingStockTests(OpeningClosingStockCase):
    def test_the_story_leaves_the_stock_it_should(self):
        # 240 + 360 - 41 + 3 - 120 + 24 - 5 - 4 - 12 + 120
        self.assertEqual(self.soap.stock, Decimal("565"))

    def test_monthly_figures_for_every_kind_of_movement(self):
        report = reporting.build_opening_closing_stock(self.month_start, self.month_end, {})
        soap = self.rows_by_product(report)["Soap"]

        self.assertEqual(soap["opening"], Decimal("240"))
        self.assertEqual(
            self.movements(soap),
            (Decimal("360"), Decimal("41"), Decimal("27"), Decimal("125"), Decimal("-4")),
        )
        self.assertEqual(soap["closing"], Decimal("457"))
        self.assertEqual(soap["sold_note"], "incl. 1 Box free")
        self.assertEqual((soap["opening_value"], soap["closing_value"]), (Decimal("2400.00"), Decimal("4570.00")))
        self.assert_rows_tie(report)

        # Quantities read in packs: 457 pcs = 3 Ctn 8 Box 1 pcs.
        self.assertEqual(soap["packer"](soap["closing"]), "3 Ctn 8 Box 1 pcs")

    def test_daily_figures_and_each_day_opens_on_the_day_before(self):
        first = self.rows_by_product(reporting.build_opening_closing_stock(self.day1, self.day1, {}))["Soap"]
        self.assertEqual((first["opening"], first["closing"]), (Decimal("240"), Decimal("562")))
        self.assertEqual(self.movements(first), (Decimal("360"), Decimal("41"), Decimal("3"), ZERO, ZERO))

        quiet_day = self.day1 + timedelta(days=1)
        quiet = self.rows_by_product(reporting.build_opening_closing_stock(quiet_day, quiet_day, {}))["Soap"]
        self.assertEqual((quiet["opening"], quiet["closing"]), (Decimal("562"), Decimal("562")))
        self.assertEqual(self.movements(quiet), (ZERO,) * 5)

        second_report = reporting.build_opening_closing_stock(self.day2, self.day2, {})
        second = self.rows_by_product(second_report)["Soap"]
        self.assertEqual((second["opening"], second["closing"]), (Decimal("562"), Decimal("457")))
        # Only the good half of the market return is stock again; the damaged carton is held for a claim.
        self.assertEqual(self.movements(second), (ZERO, ZERO, Decimal("24"), Decimal("125"), Decimal("-4")))
        self.assert_rows_tie(second_report)

    def test_closing_for_today_is_the_stock_on_hand(self):
        report = reporting.build_opening_closing_stock(self.today, self.today, {})
        rows = self.rows_by_product(report)

        self.assertEqual(rows["Soap"]["opening"], Decimal("457"))
        self.assertEqual(self.movements(rows["Soap"]), (Decimal("120"), Decimal("12"), ZERO, ZERO, ZERO))
        self.assertEqual(rows["Soap"]["closing"], self.soap.stock)
        self.assertEqual(rows["Biscuit"]["closing"], Decimal("50"))
        self.assert_rows_tie(report)

        this_month = reporting.build_opening_closing_stock(self.today.replace(day=1), reporting._month_end(self.today), {})
        self.assertEqual(self.rows_by_product(this_month)["Soap"]["closing"], self.soap.stock)
        self.assert_rows_tie(this_month)

    def test_movements_after_the_period_are_ignored(self):
        before = reporting.build_opening_closing_stock(self.month_start - timedelta(days=1), self.month_start - timedelta(days=1), {})
        soap = self.rows_by_product(before)["Soap"]
        self.assertEqual((soap["opening"], soap["closing"]), (Decimal("240"), Decimal("240")))
        self.assertEqual(self.movements(soap), (ZERO,) * 5)

    def test_a_stock_return_after_the_period_is_rolled_back_too(self):
        create_stock_return(
            kind=StockReturn.FROM_CUSTOMER, customer=self.customer, reason="Market return", user=self.admin_user,
            lines=[self.return_line(1, PACK_BOX, 144), self.return_line(1, PACK_BOX, 144, StockReturnItem.DAMAGED)],
        )
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.stock, Decimal("577"))

        yesterday = self.today - timedelta(days=1)
        closing = self.rows_by_product(reporting.build_opening_closing_stock(yesterday, yesterday, {}))["Soap"]["closing"]
        self.assertEqual(closing, Decimal("457"))
        valuation = reporting.build_stock_valuation(None, yesterday, {})
        self.assertEqual(next(row for row in valuation.rows if row["product"] == "Soap")["quantity"], Decimal("457"))
        today = self.rows_by_product(reporting.build_opening_closing_stock(self.today, self.today, {}))["Soap"]
        self.assertEqual((today["returned_in"], today["closing"]), (Decimal("12"), Decimal("577")))

    def test_days_follow_local_midnight(self):
        # 00:15 in Karachi is still the evening before in UTC.
        adjustment = create_stock_adjustment(
            product=self.soap, adjustment_mode=StockAdjustment.ADJUSTMENT, reason="Found", quantity_delta=Decimal("1"),
            actor=self.admin_user,
        )
        StockAdjustment.objects.filter(pk=adjustment.pk).update(created_at=at_local(self.month_start, 0, 15))

        eve = self.month_start - timedelta(days=1)
        self.assertEqual(self.rows_by_product(reporting.build_opening_closing_stock(eve, eve, {}))["Soap"]["closing"], Decimal("240"))
        first = self.rows_by_product(reporting.build_opening_closing_stock(self.month_start, self.month_start, {}))["Soap"]
        self.assertEqual((first["opening"], first["adjusted"], first["closing"]), (Decimal("240"), Decimal("1"), Decimal("241")))

    def test_idle_products_are_hidden_unless_all_are_asked_for(self):
        report = reporting.build_opening_closing_stock(self.month_start, self.month_end, {})
        self.assertEqual(set(self.rows_by_product(report)), {"Soap", "Biscuit"})
        self.assertEqual(report.totals["opening_value"], Decimal("3400.00"))
        self.assertEqual(report.totals["closing_value"], Decimal("5570.00"))
        self.assertEqual([card["value"] for card in report.cards[:3]], [Decimal("3400.00"), Decimal("5570.00"), Decimal("2170.00")])

        everything = reporting.build_opening_closing_stock(self.month_start, self.month_end, {"show": "all"})
        self.assertEqual(set(self.rows_by_product(everything)), {"Soap", "Biscuit", "Idle"})
        self.assert_rows_tie(everything)

    def test_category_and_product_filters(self):
        by_category = reporting.build_opening_closing_stock(self.month_start, self.month_end, {"category": str(self.category.id)})
        self.assertEqual(list(self.rows_by_product(by_category)), ["Soap"])

        one = reporting.build_opening_closing_stock(self.month_start, self.month_end, {"product": str(self.soap.id)})
        self.assertEqual(list(self.rows_by_product(one)), ["Soap"])
        # One product: its quantities total too.
        self.assertEqual((one.totals["opening"], one.totals["closing"]), (Decimal("240"), Decimal("457")))
        self.assertEqual(one.totals["opening_value"], Decimal("2400.00"))

    def test_page_renders_daily_and_monthly_and_csv_keeps_plain_numbers_and_units(self):
        url = reverse("report_opening_closing_stock")
        daily = self.client.get(url, {"period": "day", "day": self.day1.isoformat()})
        self.assertEqual(daily.status_code, 200)
        self.assertContains(daily, "Opening and Closing Stock")
        self.assertContains(daily, "4 Ctn 6 Box 10 pcs")  # 562 pcs closing
        self.assertContains(daily, 'type="date" name="day"')

        monthly = self.client.get(url, {"period": "month", "month": self.month_start.strftime("%Y-%m")})
        self.assertEqual(monthly.status_code, 200)
        self.assertEqual((monthly.context["start"], monthly.context["end"]), (self.month_start, self.month_end))
        self.assertContains(monthly, 'type="month" name="month"')
        self.assertContains(monthly, "3 Ctn 8 Box 1 pcs")

        # Switching Daily to Monthly keeps the month of the day already chosen.
        switched = self.client.get(url, {"period": "month", "day": self.day2.isoformat()})
        self.assertEqual(switched.context["start"], self.month_start)

        export = self.client.get(url, {"period": "month", "month": self.month_start.strftime("%Y-%m"), "export": "csv"})
        self.assertEqual(export.status_code, 200)
        lines = read_csv(export)
        header = lines[0]
        self.assertEqual(header[:4], ["Product", "Category", "Opening Stock", "Unit"])
        soap = next(line for line in lines if line[0] == "Soap")
        row = dict(zip(header, soap))
        self.assertEqual((row["Opening Stock"], row["Unit"], row["Purchased"], row["Sold"]), ("240", "pcs", "360", "41"))
        self.assertEqual((row["Adjusted +/-"], row["Closing Stock"], row["Closing Value"]), ("-4", "457", "4570.00"))
        self.assertEqual(lines[-1][0], "Total")

    def test_reports_home_and_products_list_link_to_it(self):
        home = self.client.get(reverse("reports_home"))
        self.assertContains(home, reverse("report_opening_closing_stock"))
        self.assertContains(home, reverse("report_stock_day_by_day"))
        products = self.client.get(reverse("product_list"))
        self.assertContains(products, reverse("report_opening_closing_stock"))


class StockCardTiesToStockTests(OpeningClosingStockCase):
    def test_stock_card_closing_is_the_product_stock_and_lists_stock_returns(self):
        report = reporting.build_stock_card(None, None, {"product": str(self.soap.id)})

        self.assertEqual(report.totals["balance"], self.soap.stock)
        self.assertEqual(report.rows[-1]["balance"], self.soap.stock)
        # Every movement is on the card, so it opens on nothing at all.
        self.assertEqual(report.rows[0]["balance"], Decimal("240"))
        types = [row["type"] for row in report.rows]
        self.assertIn("Market return", types)
        self.assertIn("Return to supplier", types)
        market = next(row for row in report.rows if row["type"] == "Market return")
        self.assertEqual(market["qty_in"], Decimal("24"))  # the damaged carton is not stock

    def test_stock_card_for_last_month_closes_on_the_report_figure(self):
        card = reporting.build_stock_card(self.month_start, self.month_end, {"product": str(self.soap.id)})
        self.assertEqual(card.totals["balance"], Decimal("457"))
        self.assertEqual(card.rows[0]["balance"] - (card.rows[0]["qty_in"] or ZERO) + (card.rows[0]["qty_out"] or ZERO), Decimal("240"))

    def test_stock_valuation_as_of_last_month_end_counts_stock_returns(self):
        report = reporting.build_stock_valuation(None, self.month_end, {})
        soap = next(row for row in report.rows if row["product"] == "Soap")
        self.assertEqual(soap["quantity"], Decimal("457"))


class StockDayByDayTests(OpeningClosingStockCase):
    def test_each_day_of_last_month_with_totals_that_match_the_monthly_report(self):
        report = reporting.build_stock_by_period(self.month_start, self.month_end, {})
        monthly = reporting.build_opening_closing_stock(self.month_start, self.month_end, {})

        self.assertEqual(len(report.rows), (self.month_end - self.month_start).days + 1)
        for row in report.rows:
            self.assertEqual(row["opening_value"] + row["stock_in"] - row["stock_out"], row["closing_value"], row["period"])
        for earlier, later in zip(report.rows, report.rows[1:]):
            self.assertEqual(earlier["closing_value"], later["opening_value"])

        self.assertEqual(report.totals["opening_value"], monthly.totals["opening_value"])
        self.assertEqual(report.totals["closing_value"], monthly.totals["closing_value"])
        # Soap 360 + 3 in and 41 out on day 1; 24 + 3 in, 120 + 5 + 7 out on day 2. Biscuit never moves.
        day1 = report.rows[(self.day1 - self.month_start).days]
        day2 = report.rows[(self.day2 - self.month_start).days]
        self.assertEqual((day1["opening_value"], day1["stock_in"], day1["stock_out"]), (Decimal("3400.00"), Decimal("3630.00"), Decimal("410.00")))
        self.assertEqual((day2["stock_in"], day2["stock_out"], day2["closing_value"]), (Decimal("270.00"), Decimal("1320.00"), Decimal("5570.00")))
        self.assertEqual(report.totals["stock_in"], Decimal("3900.00"))
        self.assertEqual(report.totals["stock_out"], Decimal("1730.00"))

        # Each line opens the per-product report for that day.
        self.assertIn(f"day={self.day1.isoformat()}", day1["period_url"])
        response = self.client.get(day1["period_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["start"], self.day1)

    def test_each_month_of_a_year_stops_at_today(self):
        year_start = self.today.replace(month=1, day=1)
        report = reporting.build_stock_by_period(year_start, year_start.replace(month=12, day=31), {})

        self.assertEqual(len(report.rows), self.today.month)
        self.assertEqual(report.rows[-1]["closing_value"], Decimal("5650.00") + Decimal("1000.00"))
        for earlier, later in zip(report.rows, report.rows[1:]):
            self.assertEqual(earlier["closing_value"], later["opening_value"])
        self.assertIn("period=month", report.rows[-1]["period_url"])

    def test_page_and_csv(self):
        url = reverse("report_stock_day_by_day")
        page = self.client.get(url, {"period": "month", "month": self.month_start.strftime("%Y-%m")})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Stock Day by Day")
        self.assertContains(page, self.day1.strftime("%a %d %b"))

        yearly = self.client.get(url, {"period": "year", "year": str(self.today.year)})
        self.assertEqual(yearly.status_code, 200)
        self.assertContains(yearly, 'type="number" name="year"')

        export = self.client.get(url, {"period": "month", "month": self.month_start.strftime("%Y-%m"), "export": "csv"})
        lines = read_csv(export)
        self.assertEqual(lines[0], ["Day", "Opening Value", "Stock In", "Stock Out", "Closing Value", "Net Change"])
        self.assertEqual(lines[-1][0], "Total")
        self.assertEqual(lines[-1][4], "5570.00")

