"""Recovery entry, the Distribution reports, the masters, and switching the module off."""
import csv
import io
from decimal import Decimal

from django.urls import reverse

from accounts.models import AuditLog, Settlement
from pos.services import modules
from pos.services.reporting import REPORTS, report_navigation

from distribution.models import DeliveryMan, Recovery, StockReturn, StockReturnItem, Vehicle
from distribution.services.loads import add_invoices, close_load, create_load, dispatch_load, post_load_returns
from distribution.services.orders import invoice_order
from distribution.services.recovery import record_recovery
from distribution.services.stock_returns import create_stock_return

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, DistributionCase

DISTRIBUTION_REPORTS = ("order-booking", "load-sheets", "delivery-settlement", "recovery", "stock-returns", "damaged-claims")


def read_csv(response):
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


class RecoveryEntryTests(DistributionCase):
    def setUp(self):
        super().setUp()
        invoice_order(self.book(self.shop, (self.soap, 1, PACK_CARTON)), user=self.admin_user)  # owes 1,440
        invoice_order(self.book(self.other_shop, (self.biscuit, 10, PACK_PIECE)), user=self.admin_user)  # owes 300

    def test_route_sheet_lists_who_owes_and_posts_receipts(self):
        page = self.client.get(reverse("distribution_recovery"), {"route": self.route.id})
        self.assertEqual([customer.name for customer in page.context["customers"]], ["Ali Store"])

        response = self.client.post(
            reverse("distribution_recovery"),
            {
                "collected_on": self.today.isoformat(), "collected_by": Recovery.BY_ORDER_BOOKER, "collector_booker": self.booker.id,
                f"amount_{self.shop.id}": "1000", f"method_{self.shop.id}": "cash",
                f"amount_{self.other_shop.id}": "300", f"method_{self.other_shop.id}": "cash",
            },
            follow=True,
        )

        self.assertTrue(any("2 recoveries recorded" in text for text in self.messages_of(response)), self.messages_of(response))
        self.assertEqual(self.balance(self.shop), Decimal("440.00"))
        self.assertEqual(self.balance(self.other_shop), Decimal("0.00"))
        self.assertEqual(Settlement.objects.filter(settlement_type=Settlement.RECEIPT).count(), 2)
        recovery = Recovery.objects.get(customer=self.shop)
        self.assertEqual((recovery.collector_name, recovery.collector_booker, recovery.route), ("Imran", self.booker, self.route))
        self.assertEqual(self.account_balance("Cash"), Decimal("1300.00"))
        self.assertTrue(AuditLog.objects.filter(action="recovery_recorded").exists())
        self.assert_books_balance()

    def test_one_bad_amount_saves_nothing(self):
        response = self.client.post(
            reverse("distribution_recovery"),
            {"collected_by": Recovery.BY_OFFICE, "collector_name": "Recovery man", f"amount_{self.shop.id}": "500", f"amount_{self.other_shop.id}": "abc"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Recovery.objects.exists())
        self.assertFalse(Settlement.objects.exists())
        self.assertTrue(any("Nothing was saved" in text for text in self.messages_of(response)))

    def test_collector_must_be_named(self):
        response = self.client.post(reverse("distribution_recovery"), {"collected_by": Recovery.BY_DELIVERY_MAN, f"amount_{self.shop.id}": "10"})
        self.assertFalse(Recovery.objects.exists())
        self.assertTrue(any("delivery man" in text for text in self.messages_of(response)))

    def test_recovery_list_filters_by_person(self):
        record_recovery(customer=self.shop, amount=Decimal("100"), collected_by=Recovery.BY_OFFICE, collector_name="Shahid")
        record_recovery(customer=self.other_shop, amount=Decimal("50"), collected_by=Recovery.BY_DELIVERY_MAN, delivery_man=self.driver)
        page = self.client.get(reverse("distribution_recovery_list"), {"collector": "Shahid"})
        self.assertEqual(page.context["total"], Decimal("100.00"))
        self.assertEqual(len(page.context["recoveries"]), 1)


class MasterListTests(DistributionCase):
    def test_delivery_men_and_vehicles_are_added_edited_and_added_in_place(self):
        self.client.post(reverse("distribution_delivery_man_add"), {"name": "Javed", "phone": "0300", "is_active": "on"})
        javed = DeliveryMan.objects.get(name="Javed")
        self.client.post(reverse("distribution_delivery_man_edit", args=[javed.id]), {"name": "Javed", "phone": "0301"})
        javed.refresh_from_db()
        self.assertEqual((javed.phone, javed.is_active), ("0301", False))
        duplicate = self.client.post(reverse("distribution_delivery_man_add"), {"name": "aslam", "is_active": "on"})
        self.assertEqual(duplicate.status_code, 200)

        quick = self.client.post(reverse("distribution_quick_vehicle"), {"quick_vehicle-name": "Loader 2", "quick_vehicle-number": "lhr-9"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(quick.json()["vehicle"]["name"], "Loader 2 (LHR-9)")
        self.assertTrue(Vehicle.objects.filter(number="LHR-9").exists())
        quick_man = self.client.post(reverse("distribution_quick_delivery_man"), {"quick_delivery_man-name": "Naveed"})
        self.assertEqual(quick_man.json()["delivery_man"]["name"], "Naveed")
        quick_customer = self.client.post(reverse("distribution_quick_customer"), {"quick_customer-name": "New Shop"})
        self.assertEqual(quick_customer.json()["customer"]["name"], "New Shop")

        for name in ("distribution_delivery_man_list", "distribution_vehicle_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)
        form = self.client.get(reverse("distribution_load_add"))
        self.assertContains(form, 'data-rf-quick="delivery_man"')
        self.assertContains(form, 'data-rf-quick="vehicle"')


class DistributionReportTests(DistributionCase):
    def make_a_day(self):
        sale = invoice_order(self.book(self.shop, (self.soap, 4, PACK_CARTON), (self.soap, 25, PACK_BOX)), user=self.admin_user)
        self.assertTrue(sale.ok, sale.error)
        self.book(self.other_shop, (self.biscuit, 1, PACK_PIECE))
        load = create_load(load_date=self.today, route=self.route, delivery_man=self.driver, user=self.admin_user)
        add_invoices(load, [sale.sale.id])
        dispatch_load(load)
        item = sale.sale.items.filter(unit_level=PACK_BOX).get()
        post_load_returns(load, {sale.sale.id: {"quantities": {item.id: 1}}}, reason="Refused")
        record_recovery(customer=self.shop, amount=Decimal("1000"), load_form=load, collected_by=Recovery.BY_DELIVERY_MAN, delivery_man=self.driver)
        close_load(load, cash_received=Decimal("990"))
        create_stock_return(
            kind=StockReturn.FROM_CUSTOMER, customer=self.shop, reason="Expired", user=self.admin_user,
            lines=[{"product": self.biscuit, "quantity": Decimal("2"), "unit_level": PACK_PIECE, "unit_factor": Decimal("1"), "rate": Decimal("30"), "condition": StockReturnItem.DAMAGED}],
        )
        return sale, load

    def test_every_distribution_report_renders_and_exports(self):
        self.make_a_day()
        specs = [spec for spec in REPORTS if spec.slug in DISTRIBUTION_REPORTS]
        self.assertEqual(len(specs), len(DISTRIBUTION_REPORTS))
        for spec in specs:
            response = self.client.get(reverse(spec.url_name))
            self.assertEqual(response.status_code, 200, spec.slug)
            self.assertContains(response, spec.title)
            export = self.client.get(reverse(spec.url_name), {"export": "csv"})
            self.assertTrue(export.content.startswith(b"\xef\xbb\xbf"), spec.slug)
            self.assertGreater(len(read_csv(export)), 1, spec.slug)

    def test_report_figures(self):
        sale, load = self.make_a_day()
        booking = self.client.get(reverse("report_order_booking")).context["report"]
        imran = next(row for row in booking.rows if row["group"] == "Imran")
        # 4 cartons + 25 boxes booked; only 5 cartons on the shelf: 10 boxes went out, 15 were short.
        self.assertEqual((imran["booked"], imran["invoiced"], imran["short"]), (Decimal("9360.00"), Decimal("7200.00"), Decimal("2160.00")))
        self.assertEqual(booking.totals["pending"], Decimal("30.00"))

        settlement = self.client.get(reverse("report_delivery_settlement")).context["report"]
        row = settlement.rows[0]
        self.assertEqual((row["delivery_man"], row["recovered"], row["cash_expected"], row["cash_received"], row["variance"]), ("Aslam", Decimal("1000.00"), Decimal("1000.00"), Decimal("990.00"), Decimal("-10.00")))
        self.assertEqual(row["returns"], Decimal("144.00"))

        by_person = self.client.get(reverse("report_recovery"), {"by": "person"}).context["report"]
        self.assertEqual([(row["group"], row["total"]) for row in by_person.rows], [("Aslam", Decimal("1000.00"))])
        by_route = self.client.get(reverse("report_recovery"), {"by": "route"}).context["report"]
        self.assertEqual(by_route.rows[0]["group"], self.route.label)

        sheets = self.client.get(reverse("report_load_sheets")).context["report"]
        self.assertEqual(sheets.rows[0]["number"], load.display_number)

        claims = self.client.get(reverse("report_damaged_claims")).context["report"]
        self.assertEqual([(row["product"], row["held_qty"], row["held_cost"]) for row in claims.rows], [("Biscuit", Decimal("2.000"), Decimal("40.00"))])
        returns = self.client.get(reverse("report_stock_returns"), {"condition": "damaged"}).context["report"]
        self.assertEqual(len(returns.rows), 1)

    def test_distribution_pages_render(self):
        sale, _ = self.make_a_day()
        self.assertContains(self.client.get(reverse("distribution_order_detail", args=[sale.order.id])), "Short")
        booked = self.book(self.shop, (self.biscuit, 1, PACK_PIECE))
        self.assertContains(self.client.get(reverse("distribution_order_detail", args=[booked.id])), "Make invoice")
        self.assertEqual(self.client.get(reverse("distribution_order_edit", args=[booked.id])).status_code, 200)
        second = invoice_order(self.book(self.shop, (self.soap, 1, PACK_BOX)), user=self.admin_user).sale
        out = create_load(load_date=self.today, user=self.admin_user)
        add_invoices(out, [second.id])
        dispatch_load(out)
        settle = self.client.get(reverse("distribution_load_settle", args=[out.id]))
        self.assertContains(settle, "Post returns")
        self.assertContains(settle, "Close the load")
        self.assertContains(self.client.get(reverse("distribution_return_add", args=["customer"]), {"load": out.id}), "Came back on load")
        for name in ("distribution_order_list", "distribution_load_list", "distribution_return_list", "distribution_recovery", "distribution_recovery_list"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertRedirects(self.client.get(reverse("distribution_home")), reverse("distribution_order_list"))


class ModuleSwitchTests(DistributionCase):
    def test_module_off_hides_everything(self):
        on = self.client.get(reverse("dashboard"))
        self.assertContains(on, reverse("distribution_home"))
        self.assertIn("Distribution", [group["name"] for group in report_navigation()])

        modules.set_module_switch("distribution", False)

        page = self.client.get(reverse("dashboard"))
        self.assertNotContains(page, reverse("distribution_home"))
        for name in ("distribution_order_list", "distribution_load_list", "distribution_recovery", "distribution_return_list", "report_order_booking", "report_recovery"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
        posted = self.client.post(reverse("distribution_order_add"), {"customer": self.shop.id})
        self.assertEqual(posted.status_code, 302)
        self.assertNotIn("Distribution", [group["name"] for group in report_navigation()])
        home = self.client.get(reverse("reports_home"))
        self.assertNotContains(home, "Order Booking Summary")

    def test_customers_module_off_closes_distribution_too(self):
        modules.set_module_switch("parties", False)
        self.assertEqual(self.client.get(reverse("distribution_order_list")).status_code, 302)
        self.assertNotContains(self.client.get(reverse("dashboard")), reverse("distribution_home"))

    def test_a_cashier_cannot_open_distribution(self):
        from django.contrib.auth import get_user_model

        from pos.services.access import CASHIER_ROLE, assign_role

        cashier = get_user_model().objects.create_user(username="till", password="CashierPass123!")
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)
        self.assertEqual(self.client.get(reverse("distribution_order_list")).status_code, 302)
