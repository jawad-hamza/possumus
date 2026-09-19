import csv
import io
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from accounts.models import PartyLedgerEntry, Voucher
from pos.models import (
    Category,
    Customer,
    Product,
    Purchase,
    PurchaseItem,
    Sale,
    SaleItem,
    SaleReturn,
    StockAdjustment,
    Supplier,
    SystemSettings,
)
from pos.services import reporting
from pos.services.returns import create_purchase_return, create_sale_return
from pos.services.stock_control import create_stock_adjustment
from pos.test_support import AuthenticatedAdminTestCase


def at_local_noon(day):
    return timezone.make_aware(datetime.combine(day, time(12, 0)), timezone.get_current_timezone())


def read_csv(response):
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


class ReportTestCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.category = Category.objects.create(name="Grocery")
        self.flour = Product.objects.create(
            name="Flour",
            sku="FLR-1",
            unit="kg",
            category=self.category,
            cost_price=Decimal("100.00"),
            price=Decimal("150.00"),
            stock=Decimal("50.000"),
        )
        self.soap = Product.objects.create(
            name="Soap",
            sku="SOAP-1",
            cost_price=Decimal("20.00"),
            price=Decimal("50.00"),
            stock=Decimal("40.000"),
        )

    def sell(self, product, quantity, *, day=None, customer=None, is_credit=False, payment_method="cash",
             discount_rate="0", tax_rate="0", sold_by=None, paid=None):
        sale = Sale.objects.create(
            customer=customer,
            is_credit=is_credit,
            payment_method=payment_method,
            discount_rate=Decimal(discount_rate),
            tax_rate=Decimal(tax_rate),
            sold_by=sold_by or self.admin_user,
        )
        item = SaleItem.objects.create(
            sale=sale, product=product, quantity=Decimal(quantity), price=product.price, cost_price=product.cost_price
        )
        sale.refresh_from_db()
        sale.paid_amount = sale.total_amount if paid is None else Decimal(paid)
        sale.save(update_fields=["paid_amount"])
        if day is not None:
            Sale.objects.filter(pk=sale.pk).update(created_at=at_local_noon(day))
            sale.refresh_from_db()
        return sale, item

    def give_back(self, sale, item, quantity, *, day=None, refund_method="cash"):
        sale_return = create_sale_return(
            sale=sale,
            quantities={item.id: str(quantity)},
            refund_method=refund_method,
            reason="Changed mind",
            actor=self.admin_user,
        )
        if day is not None:
            SaleReturn.objects.filter(pk=sale_return.pk).update(created_at=at_local_noon(day))
        return sale_return


class SalesTotalsTests(ReportTestCase):
    def test_totals_are_net_of_returns_for_net_tax_and_cost(self):
        sale, item = self.sell(self.soap, 4, discount_rate="10", tax_rate="10")
        # 4 x 50 = 200, less 10% = 180 net, 18 tax, 198 total, cost 80.
        self.give_back(sale, item, 1)
        # One piece back: 45 net, 4.50 tax, 49.50 total, 20 cost.

        totals = reporting.sales_totals(self.today, self.today)

        self.assertEqual(totals["invoices"], 1)
        self.assertEqual(totals["gross_sales"], Decimal("180.00"))
        self.assertEqual(totals["returns_net"], Decimal("45.00"))
        self.assertEqual(totals["net_sales"], Decimal("135.00"))
        self.assertEqual(totals["tax"], Decimal("13.50"))
        self.assertEqual(totals["total"], Decimal("148.50"))
        self.assertEqual(totals["cost"], Decimal("60.00"))
        self.assertEqual(totals["gross_profit"], Decimal("75.00"))
        self.assertEqual(totals["paid"], Decimal("148.50"))
        self.assertEqual(totals["returns_count"], 1)

    def test_receivable_return_lowers_what_is_due_not_what_was_paid(self):
        customer = Customer.objects.create(name="Ali")
        sale, item = self.sell(self.soap, 2, customer=customer, is_credit=True, paid="0")
        self.give_back(sale, item, 1, refund_method="receivable")

        totals = reporting.sales_totals(self.today, self.today)

        self.assertEqual(totals["paid"], Decimal("0.00"))
        self.assertEqual(totals["due"], Decimal("50.00"))

    def test_return_counts_in_the_period_it_was_recorded(self):
        last_week = self.today - timedelta(days=10)
        sale, item = self.sell(self.soap, 2, day=last_week)
        self.give_back(sale, item, 1)

        earlier = reporting.sales_totals(last_week, last_week)
        now = reporting.sales_totals(self.today, self.today)

        self.assertEqual(earlier["net_sales"], Decimal("100.00"))
        self.assertEqual(now["invoices"], 0)
        self.assertEqual(now["net_sales"], Decimal("-50.00"))
        self.assertEqual(now["gross_profit"], Decimal("-30.00"))

    def test_weights_are_costed_by_the_fraction_sold(self):
        self.sell(self.flour, "0.250")

        totals = reporting.sales_totals(self.today, self.today)

        self.assertEqual(totals["net_sales"], Decimal("37.50"))
        self.assertEqual(totals["cost"], Decimal("25.00"))

    def test_periods_are_split_on_the_local_date(self):
        yesterday = self.today - timedelta(days=1)
        self.sell(self.soap, 1, day=yesterday)
        self.sell(self.soap, 2)

        totals = reporting.sales_totals_by_period(
            {"today": (self.today, self.today), "both": (yesterday, self.today)}
        )

        self.assertEqual(totals["today"]["net_sales"], Decimal("100.00"))
        self.assertEqual(totals["both"]["net_sales"], Decimal("150.00"))
        self.assertEqual(totals["both"]["invoices"], 2)

    def test_a_sale_just_after_local_midnight_belongs_to_the_new_day(self):
        sale, _ = self.sell(self.soap, 1)
        just_after_midnight = timezone.make_aware(
            datetime.combine(self.today, time(0, 5)), timezone.get_current_timezone()
        )
        Sale.objects.filter(pk=sale.pk).update(created_at=just_after_midnight)

        self.assertEqual(reporting.sales_totals(self.today, self.today)["invoices"], 1)
        yesterday = self.today - timedelta(days=1)
        self.assertEqual(reporting.sales_totals(yesterday, yesterday)["invoices"], 0)

    def test_query_count_does_not_grow_with_the_number_of_sales(self):
        for _ in range(6):
            self.sell(self.soap, 1)
        with self.assertNumQueries(3):
            reporting.sales_totals_by_period(
                {"today": (self.today, self.today), "week": (self.today - timedelta(days=6), self.today)}
            )

    def test_trend_nets_returns_and_uses_three_queries(self):
        yesterday = self.today - timedelta(days=1)
        sale, item = self.sell(self.soap, 2, day=yesterday)
        self.sell(self.soap, 1)
        self.give_back(sale, item, 1)

        with self.assertNumQueries(3):
            trend = reporting.sales_trend(self.today, days=7)

        self.assertEqual(len(trend["labels"]), 7)
        self.assertEqual(trend["net_sales"][-2], 100.0)
        # Today: one sale of 50 less a return of 50.
        self.assertEqual(trend["net_sales"][-1], 0.0)
        self.assertEqual(trend["gross_profit"][-1], 0.0)


class DateRangeLinkTests(ReportTestCase):
    def test_quick_range_links_use_the_parameters_the_report_reads(self):
        week_start = self.today - timedelta(days=6)
        url = reporting.date_range_url("daily_sales_report", week_start, self.today)
        self.assertIn(f"start={week_start.isoformat()}", url)
        self.assertIn(f"end={self.today.isoformat()}", url)

    def test_last_seven_days_link_shows_the_whole_week(self):
        three_days_ago = self.today - timedelta(days=3)
        self.sell(self.soap, 1, day=three_days_ago)
        self.sell(self.soap, 1)

        dashboard = self.client.get(reverse("dashboard"))
        week_kpi = next(kpi for kpi in dashboard.context["dashboard_kpis"] if kpi["title"] == "Last 7 Days Sales")
        response = self.client.get(week_kpi["url"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["start"], self.today - timedelta(days=6))
        self.assertEqual(response.context["end"], self.today)
        self.assertEqual(response.context["totals"]["invoices"], 2)
        self.assertEqual(week_kpi["value"], Decimal("100.00"))

    def test_old_from_to_bookmarks_still_work(self):
        three_days_ago = self.today - timedelta(days=3)
        self.sell(self.soap, 1, day=three_days_ago)
        response = self.client.get(
            reverse("daily_sales_report"), {"from": three_days_ago.isoformat(), "to": self.today.isoformat()}
        )
        self.assertEqual(response.context["totals"]["invoices"], 1)

    def test_report_defaults_to_today_and_swaps_reversed_dates(self):
        response = self.client.get(reverse("daily_sales_report"))
        self.assertEqual(response.context["start"], self.today)
        self.assertEqual(response.context["end"], self.today)

        earlier = self.today - timedelta(days=5)
        response = self.client.get(
            reverse("daily_sales_report"), {"start": self.today.isoformat(), "end": earlier.isoformat()}
        )
        self.assertEqual(response.context["start"], earlier)
        self.assertEqual(response.context["end"], self.today)

    def test_switcher_links_carry_the_chosen_dates(self):
        start = self.today - timedelta(days=6)
        response = self.client.get(
            reverse("daily_sales_report"), {"start": start.isoformat(), "end": self.today.isoformat()}
        )
        links = {link["slug"]: link["url"] for group in response.context["report_groups"] for link in group["links"]}
        self.assertIn(f"start={start.isoformat()}", links["sales-by-product"])
        self.assertIn(f"end={self.today.isoformat()}", links["stock-valuation"])
        self.assertNotIn("start=", links["low-stock"])


class DashboardAndReportsHomeTests(ReportTestCase):
    def test_dashboard_and_reports_home_show_the_same_net_figures(self):
        sale, item = self.sell(self.soap, 4)
        self.give_back(sale, item, 1)

        dashboard = self.client.get(reverse("dashboard"))
        reports_home = self.client.get(reverse("reports_home"))

        self.assertEqual(dashboard.context["today_sales"]["net_sales"], Decimal("150.00"))
        self.assertEqual(dashboard.context["today_sales"]["gross_profit"], Decimal("90.00"))
        today_kpi = next(kpi for kpi in reports_home.context["report_kpis"] if kpi["title"] == "Today Sales")
        self.assertEqual(today_kpi["value"], Decimal("150.00"))
        best = {row["product__name"]: row["total_qty"] for row in dashboard.context["best_selling"]}
        self.assertEqual(best["Soap"], Decimal("3.000"))

    def test_dashboard_party_balances_come_from_one_grouped_query_each(self):
        customer_type = ContentType.objects.get_for_model(Customer)
        supplier_type = ContentType.objects.get_for_model(Supplier)
        voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=self.today, narration="Balances")
        for index in range(5):
            customer = Customer.objects.create(name=f"Customer {index}")
            PartyLedgerEntry.objects.create(voucher=voucher, content_type=customer_type, object_id=customer.id, debit=Decimal("100.00"))
            supplier = Supplier.objects.create(name=f"Supplier {index}")
            PartyLedgerEntry.objects.create(voucher=voucher, content_type=supplier_type, object_id=supplier.id, credit=Decimal("40.00"))
        inactive = Customer.objects.create(name="Gone", is_active=False)
        PartyLedgerEntry.objects.create(voucher=voucher, content_type=customer_type, object_id=inactive.id, debit=Decimal("999.00"))

        ContentType.objects.get_for_model(Customer)  # warm the content type cache
        with self.assertNumQueries(1):
            summary = reporting.party_balance_summary(Customer)
        self.assertEqual(summary["owed_to_us"], Decimal("500.00"))
        self.assertEqual(summary["owed_to_us_count"], 5)

        response = self.client.get(reverse("dashboard"))
        kpis = {kpi["title"]: kpi for kpi in response.context["dashboard_kpis"]}
        self.assertEqual(kpis["Customers Owe You"]["value"], Decimal("500.00"))
        self.assertEqual(kpis["You Owe Suppliers"]["value"], Decimal("200.00"))

    def test_dashboard_queries_do_not_grow_with_parties_or_sales(self):
        def count_queries():
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as captured:
                self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
            return len(captured)

        self.sell(self.soap, 1)
        baseline = count_queries()
        for index in range(8):
            Customer.objects.create(name=f"Extra customer {index}")
            Supplier.objects.create(name=f"Extra supplier {index}")
            self.sell(self.soap, 1, day=self.today - timedelta(days=index % 5))
        self.assertEqual(count_queries(), baseline)


class PartyListTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        customer_type = ContentType.objects.get_for_model(Customer)
        voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=self.today, narration="Balances")
        self.owing = Customer.objects.create(name="Owing Omar")
        self.advance = Customer.objects.create(name="Advance Asma")
        self.clear = Customer.objects.create(name="Clear Kiran")
        PartyLedgerEntry.objects.create(voucher=voucher, content_type=customer_type, object_id=self.owing.id, debit=Decimal("300.00"))
        PartyLedgerEntry.objects.create(voucher=voucher, content_type=customer_type, object_id=self.owing.id, credit=Decimal("50.00"))
        PartyLedgerEntry.objects.create(voucher=voucher, content_type=customer_type, object_id=self.advance.id, credit=Decimal("75.00"))

    def test_customer_list_badges_match_the_ledger(self):
        response = self.client.get(reverse("customer_list"))
        badges = {customer.name: (customer.balance_type, customer.balance_amount) for customer in response.context["customers"]}
        self.assertEqual(badges["Owing Omar"], ("receivable", Decimal("250.00")))
        self.assertEqual(badges["Advance Asma"], ("advance", Decimal("75.00")))
        self.assertEqual(badges["Clear Kiran"], ("clear", Decimal("0")))
        self.assertEqual(response.context["balance_summary"]["owed_to_us"], Decimal("250.00"))

    def test_customer_list_filters_by_balance(self):
        response = self.client.get(reverse("customer_list"), {"balance": "receivable"})
        self.assertEqual([customer.name for customer in response.context["customers"]], ["Owing Omar"])

    def test_customer_list_is_paginated_without_a_query_per_customer(self):
        for index in range(30):
            Customer.objects.create(name=f"Bulk {index:02d}")
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("customer_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["customers"]), 24)
        self.assertEqual(response.context["page_obj"].paginator.count, 33)
        ledger_queries = [query for query in captured if "partyledgerentry" in query["sql"].lower()]
        self.assertLessEqual(len(ledger_queries), 3)

        page_two = self.client.get(reverse("customer_list"), {"page": 2})
        self.assertEqual(len(page_two.context["customers"]), 9)

    def test_supplier_list_shows_payables(self):
        supplier = Supplier.objects.create(name="ACME")
        voucher = Voucher.objects.create(voucher_type=Voucher.JOURNAL, date=self.today, narration="Payable")
        PartyLedgerEntry.objects.create(
            voucher=voucher, content_type=ContentType.objects.get_for_model(Supplier), object_id=supplier.id, credit=Decimal("120.00")
        )
        response = self.client.get(reverse("supplier_list"), {"balance": "payable"})
        self.assertEqual([(row.name, row.balance_type, row.balance_amount) for row in response.context["suppliers"]], [("ACME", "payable", Decimal("120.00"))])
        self.assertContains(response, "You owe suppliers")


class TableReportTests(ReportTestCase):
    def test_every_report_renders_and_exports_csv_with_a_bom(self):
        sale, item = self.sell(self.flour, "2.500", tax_rate="5")
        self.give_back(sale, item, "0.500")
        for spec in reporting.REPORTS:
            params = {"product": self.flour.id} if spec.slug == "stock-card" else {}
            response = self.client.get(reverse(spec.url_name), params)
            self.assertEqual(response.status_code, 200, spec.slug)
            self.assertContains(response, spec.title)

            export = self.client.get(reverse(spec.url_name), {**params, "export": "csv"})
            self.assertEqual(export.status_code, 200, spec.slug)
            self.assertTrue(export["Content-Type"].startswith("text/csv"), spec.slug)
            self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"), spec.slug)
            self.assertIn("attachment", export["Content-Disposition"])
            self.assertGreaterEqual(len(read_csv(export)), 1, spec.slug)

    def test_reports_require_the_reports_module(self):
        from django.contrib.auth import get_user_model
        from pos.services.access import CASHIER_ROLE, assign_role

        cashier = get_user_model().objects.create_user(username="till", password="CashierPass123!")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)
        response = self.client.get(reverse("report_stock_valuation"))
        self.assertEqual(response.status_code, 302)

    def test_sales_by_product_nets_quantity_value_and_cost(self):
        sale, item = self.sell(self.flour, "2.500", discount_rate="10")
        self.give_back(sale, item, "0.500")

        report = reporting.build_sales_by_product(self.today, self.today, {})
        row = next(row for row in report.rows if row["product"] == "Flour")

        self.assertEqual(row["sold_qty"], Decimal("2.500"))
        self.assertEqual(row["returned_qty"], Decimal("0.500"))
        self.assertEqual(row["net_qty"], Decimal("2.000"))
        self.assertEqual(row["unit"], "kg")
        # 2.5 x 150 = 375, less 10% = 337.50; returned 0.5 x 150 x 0.9 = 67.50.
        self.assertEqual(row["net_sales"], Decimal("270.00"))
        self.assertEqual(row["cost"], Decimal("200.00"))
        self.assertEqual(row["gross_profit"], Decimal("70.00"))

        export = self.client.get(reverse("report_sales_by_product"), {"export": "csv"})
        lines = read_csv(export)
        self.assertIn("Unit", lines[0])
        flour_line = next(line for line in lines if line[0] == "Flour")
        self.assertIn("kg", flour_line)
        self.assertIn("2.5", flour_line)
        self.assertEqual(lines[-1][0], "Total")

    def test_sales_by_category_and_customer(self):
        customer = Customer.objects.create(name="Bilal")
        self.sell(self.flour, 1, customer=customer)
        sale, item = self.sell(self.soap, 2)
        self.give_back(sale, item, 1)

        categories = {row["category"]: row for row in reporting.build_sales_by_category(self.today, self.today, {}).rows}
        self.assertEqual(categories["Grocery"]["net_sales"], Decimal("150.00"))
        self.assertEqual(categories["Uncategorised"]["net_sales"], Decimal("50.00"))
        self.assertEqual(categories["Uncategorised"]["returns"], Decimal("50.00"))

        customers = {row["customer"]: row for row in reporting.build_sales_by_customer(self.today, self.today, {}).rows}
        self.assertEqual(customers["Bilal"]["net_sales"], Decimal("150.00"))
        self.assertEqual(customers["Bilal"]["gross_profit"], Decimal("50.00"))
        self.assertEqual(customers["Walk-in Customer"]["net_sales"], Decimal("50.00"))
        self.assertEqual(customers["Walk-in Customer"]["returns"], Decimal("50.00"))

    def test_sales_by_payment_method_separates_credit_and_refunds(self):
        customer = Customer.objects.create(name="Credit Chaudhry")
        sale, item = self.sell(self.soap, 2)
        self.give_back(sale, item, 1)
        self.sell(self.soap, 1, payment_method="bank")
        self.sell(self.soap, 3, customer=customer, is_credit=True, paid="0")

        rows = {row["method"]: row for row in reporting.build_sales_by_payment_method(self.today, self.today, {}).rows}

        self.assertEqual(rows["Cash"]["billed"], Decimal("100.00"))
        self.assertEqual(rows["Cash"]["refunded"], Decimal("50.00"))
        self.assertEqual(rows["Cash"]["net_collected"], Decimal("50.00"))
        self.assertEqual(rows["Bank"]["collected"], Decimal("50.00"))
        self.assertEqual(rows["Credit (on account)"]["billed"], Decimal("150.00"))
        self.assertEqual(rows["Credit (on account)"]["due"], Decimal("150.00"))

    def test_tax_summary_groups_by_rate_and_ties_to_the_invoices(self):
        settings_obj = SystemSettings.load()
        settings_obj.tax_label = "VAT"
        settings_obj.save()
        sale, item = self.sell(self.soap, 2, tax_rate="10")
        Sale.objects.filter(pk=sale.pk).update(tax_label="VAT")
        other, _ = self.sell(self.soap, 1, tax_rate="5")
        Sale.objects.filter(pk=other.pk).update(tax_label="VAT")
        self.sell(self.soap, 1)
        sale.refresh_from_db()
        self.give_back(sale, item, 1)

        report = reporting.build_tax_summary(self.today, self.today, {})
        rows = {(row["label"], row["rate"]): row for row in report.rows}

        ten = rows[("VAT", Decimal("10.00"))]
        self.assertEqual(ten["taxable_sales"], Decimal("100.00"))
        self.assertEqual(ten["tax_on_sales"], Decimal("10.00"))
        self.assertEqual(ten["tax_on_returns"], Decimal("5.00"))
        self.assertEqual(ten["net_tax"], Decimal("5.00"))
        self.assertEqual(rows[("VAT", Decimal("5.00"))]["tax_on_sales"], Decimal("2.50"))
        self.assertEqual(report.totals["net_tax"], reporting.sales_totals(self.today, self.today)["tax"])
        self.assertEqual(report.totals["net_tax"], Decimal("7.50"))

    def test_stock_valuation_uses_quantity_times_average_cost_with_units(self):
        report = reporting.build_stock_valuation(None, self.today, {})
        rows = {row["product"]: row for row in report.rows}

        self.assertEqual(rows["Flour"]["quantity"], Decimal("50.000"))
        self.assertEqual(rows["Flour"]["unit"], "kg")
        self.assertEqual(rows["Flour"]["cost_value"], Decimal("5000.00"))
        self.assertEqual(report.totals["cost_value"], Decimal("5800.00"))

        response = self.client.get(reverse("report_stock_valuation"), {"category": self.category.id})
        self.assertContains(response, "50 kg")
        self.assertNotContains(response, "SOAP-1")

    def test_stock_valuation_as_of_an_earlier_date_rolls_movements_back(self):
        self.sell(self.flour, "4.000")
        yesterday = self.today - timedelta(days=1)

        report = reporting.build_stock_valuation(None, yesterday, {})
        flour = next(row for row in report.rows if row["product"] == "Flour")

        self.assertEqual(flour["quantity"], Decimal("50.000"))
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.stock, Decimal("46.000"))

    def test_stock_card_lists_every_movement_with_a_running_balance(self):
        product = Product.objects.create(name="Rice", sku="RICE-1", unit="kg", cost_price=Decimal("80.00"), price=Decimal("120.00"), stock=0)
        create_stock_adjustment(
            product=product, adjustment_mode=StockAdjustment.OPENING_STOCK, reason="Opening", quantity_delta=Decimal("10"), actor=self.admin_user
        )
        supplier = Supplier.objects.create(name="Mill")
        purchase = Purchase.objects.create(payment_method=Purchase.CASH, supplier=supplier)
        purchase_item = PurchaseItem(purchase=purchase, product=product, quantity=Decimal("20"), cost_price=Decimal("80.00"), sale_price=Decimal("120.00"))
        purchase_item.save()
        product.refresh_from_db()
        sale, item = self.sell(product, "2.500")
        self.give_back(sale, item, "0.500")
        create_purchase_return(
            purchase=purchase, quantities={purchase_item.id: "3"}, settlement_method="payable", reason="Damp", actor=self.admin_user
        )

        report = reporting.build_stock_card(self.today, self.today, {"product": str(product.id)})

        self.assertEqual([row["type"] for row in report.rows], ["Opening Stock", "Purchase", "Sale", "Sale return", "Purchase return"])
        self.assertEqual(
            [row["balance"] for row in report.rows],
            [Decimal("10.000"), Decimal("30.000"), Decimal("27.500"), Decimal("28.000"), Decimal("25.000")],
        )
        product.refresh_from_db()
        self.assertEqual(report.rows[-1]["balance"], product.stock)
        self.assertEqual(report.totals["qty_in"], Decimal("30.500"))
        self.assertEqual(report.totals["qty_out"], Decimal("5.500"))

        response = self.client.get(reverse("report_stock_card"), {"product": product.id})
        self.assertContains(response, "27.5 kg")

    def test_stock_card_without_a_product_asks_for_one(self):
        response = self.client.get(reverse("report_stock_card"))
        self.assertContains(response, "Choose a product")

    def test_returns_report_lists_sale_and_purchase_returns(self):
        sale, item = self.sell(self.soap, 2, tax_rate="10")
        self.give_back(sale, item, 1)
        supplier = Supplier.objects.create(name="ACME")
        purchase = Purchase.objects.create(payment_method=Purchase.CASH, supplier=supplier)
        purchase_item = PurchaseItem(purchase=purchase, product=self.soap, quantity=5, cost_price=Decimal("20.00"), sale_price=Decimal("50.00"))
        purchase_item.save()
        create_purchase_return(
            purchase=purchase, quantities={purchase_item.id: "2"}, settlement_method="payable", reason="Faulty", actor=self.admin_user
        )

        report = reporting.build_returns(self.today, self.today, {})
        kinds = sorted(row["kind"] for row in report.rows)
        self.assertEqual(kinds, ["Purchase return", "Sale return"])
        sale_row = next(row for row in report.rows if row["kind"] == "Sale return")
        self.assertEqual(sale_row["net"], Decimal("50.00"))
        self.assertEqual(sale_row["tax"], Decimal("5.00"))
        self.assertEqual(sale_row["total"], Decimal("55.00"))

        only_sales = reporting.build_returns(self.today, self.today, {"kind": "sale"})
        self.assertEqual(len(only_sales.rows), 1)
        self.assertEqual(only_sales.totals["total"], Decimal("55.00"))

    def test_csv_text_cannot_start_a_spreadsheet_formula(self):
        Product.objects.create(name="=HYPERLINK(1)", sku="EVIL-1", cost_price=Decimal("1.00"), price=Decimal("2.00"), stock=5)
        export = self.client.get(reverse("report_stock_valuation"), {"export": "csv"})
        names = [line[0] for line in read_csv(export)]
        self.assertIn("'=HYPERLINK(1)", names)


class StockAlertReportTests(ReportTestCase):
    def test_low_stock_shows_units_and_exports_csv(self):
        self.flour.stock = Decimal("1.500")
        self.flour.low_stock_threshold = Decimal("5.000")
        self.flour.save()

        response = self.client.get(reverse("low_stock_list"))
        self.assertContains(response, "1.5 kg")
        self.assertContains(response, "3.5 kg")

        export = self.client.get(reverse("low_stock_list"), {"export": "csv"})
        self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"))
        lines = read_csv(export)
        self.assertEqual(lines[0][:1], ["Product"])
        flour_line = next(line for line in lines if line[0] == "Flour")
        self.assertIn("1.5", flour_line)
        self.assertIn("kg", flour_line)

    def test_expiry_report_shows_quantities_with_units_and_exports_csv(self):
        settings_obj = SystemSettings.load()
        settings_obj.enable_product_expiry = True
        settings_obj.expiry_alert_days = 30
        settings_obj.save()
        self.flour.expiry_date = self.today + timedelta(days=5)
        self.flour.save()
        self.soap.expiry_date = self.today - timedelta(days=2)
        self.soap.save()

        response = self.client.get(reverse("expiry_alert_report"))
        self.assertContains(response, "50 kg")
        self.assertContains(response, "40 pcs")

        lines = read_csv(self.client.get(reverse("expiry_alert_report"), {"export": "csv"}))
        by_product = {line[1]: line for line in lines[1:]}
        self.assertEqual(by_product["Flour"][0], "Near expiry")
        self.assertEqual(by_product["Soap"][0], "Expired")
        self.assertIn("kg", by_product["Flour"])
        self.assertIn("-2", by_product["Soap"])


class CustomerDetailTests(ReportTestCase):
    def test_customer_total_sales_is_net_of_returns(self):
        customer = Customer.objects.create(name="Dania")
        sale, item = self.sell(self.soap, 3, customer=customer)
        self.give_back(sale, item, 1)

        response = self.client.get(reverse("customer_detail", args=[customer.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_sales"], Decimal("100.00"))
        self.assertEqual(response.context["total_returns"], Decimal("50.00"))
        self.assertEqual(response.context["total_transactions"], 1)


class EmployeeAndInvoiceReportTests(ReportTestCase):
    def test_employee_report_is_net_of_returns(self):
        sale, item = self.sell(self.soap, 4)
        self.give_back(sale, item, 1)

        response = self.client.get(reverse("employee_report_list"))
        row = next(row for row in response.context["employee_rows"] if row["user"].id == self.admin_user.id)
        self.assertEqual(row["sales_totals"]["net_sales"], Decimal("150.00"))
        self.assertEqual(row["sales_totals"]["gross_profit"], Decimal("90.00"))

        detail = self.client.get(reverse("employee_report_detail", args=[self.admin_user.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context["metrics"]["sales_totals"]["net_sales"], Decimal("150.00"))
        self.assertEqual(detail.context["recent_sales"][0].report_returned_total, Decimal("50.00"))

    def test_employee_list_queries_do_not_grow_with_users(self):
        from django.contrib.auth import get_user_model
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def ledger_queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get(reverse("employee_report_list"))
            return len([query for query in captured if "pos_sale" in query["sql"].lower()])

        baseline = ledger_queries()
        for index in range(5):
            user = get_user_model().objects.create_user(username=f"staff{index}", password="StaffPass123!")
            self.sell(self.soap, 1, sold_by=user)
        self.assertEqual(ledger_queries(), baseline)

    def test_sales_by_invoice_is_paginated_and_totals_cover_every_page(self):
        for _ in range(3):
            self.sell(self.soap, 1)
        from pos.views import reports as report_views

        original = report_views.SALES_PAGE_SIZE
        report_views.SALES_PAGE_SIZE = 2
        try:
            response = self.client.get(reverse("daily_sales_report"))
        finally:
            report_views.SALES_PAGE_SIZE = original

        self.assertEqual(len(response.context["sales"]), 2)
        self.assertEqual(response.context["page_obj"].paginator.num_pages, 2)
        self.assertEqual(response.context["totals"]["invoices"], 3)
        self.assertEqual(response.context["totals"]["net_sales"], Decimal("150.00"))

    def test_sales_by_invoice_csv(self):
        sale, item = self.sell(self.soap, 2, tax_rate="10")
        self.give_back(sale, item, 1)
        lines = read_csv(self.client.get(reverse("daily_sales_report"), {"export": "csv"}))
        self.assertEqual(lines[0][0], "Invoice")
        self.assertEqual(lines[1][0], reporting.sale_number(sale))
        self.assertEqual(lines[1][-1], "55.00")
