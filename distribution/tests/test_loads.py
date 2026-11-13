"""Load forms: putting invoices on a load, its constraints and the printed load sheet."""
from decimal import Decimal

from django.urls import reverse

from pos.models import Sale

from distribution.models import LoadForm, LoadFormInvoice
from distribution.services.common import DistributionError
from distribution.services.loads import add_invoices, create_load, dispatch_load, load_sheet, loadable_sales
from distribution.services.orders import invoice_order

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, DistributionCase


class LoadCase(DistributionCase):
    def invoiced(self, customer, *lines, credit=True):
        result = invoice_order(self.book(customer, *lines), user=self.admin_user, credit=credit)
        self.assertTrue(result.ok, result.error)
        return result.sale

    def new_load(self, **extra):
        values = {"load_date": self.today, "route": self.route, "delivery_man": self.driver, "vehicle": self.van, "user": self.admin_user}
        values.update(extra)
        return create_load(**values)


class LoadFormTests(LoadCase):
    def test_load_is_opened_from_the_form(self):
        response = self.client.post(
            reverse("distribution_load_add"),
            {"load_date": self.today.isoformat(), "route": self.route.id, "delivery_man": self.driver.id, "vehicle": self.van.id},
        )
        load = LoadForm.objects.get()
        self.assertRedirects(response, reverse("distribution_load_detail", args=[load.id]), fetch_redirect_response=False)
        self.assertEqual((load.status, load.delivery_man, load.vehicle), (LoadForm.OPEN, self.driver, self.van))

    def test_invoices_are_picked_by_route_and_ride_on_one_load_only(self):
        mine = self.invoiced(self.shop, (self.soap, 2, PACK_CARTON))
        theirs = self.invoiced(self.other_shop, (self.biscuit, 2, PACK_PIECE))
        load = self.new_load()

        self.assertEqual(list(loadable_sales(load_date=self.today, route=self.route)), [mine])
        self.assertEqual(set(loadable_sales(load_date=self.today)), {mine, theirs})

        response = self.client.post(reverse("distribution_load_add_invoices", args=[load.id]), {"sale": [mine.id, theirs.id]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(load.invoices.count(), 2)
        self.assertFalse(loadable_sales(load_date=self.today).exists())

        other = self.new_load(route=self.other_route)
        with self.assertRaisesMessage(DistributionError, "already on load"):
            add_invoices(other, [mine.id], user=self.admin_user)
        self.assertEqual(LoadFormInvoice.objects.filter(sale=mine).count(), 1)

    def test_walk_in_sales_cannot_go_on_a_load_and_a_dispatched_load_takes_no_more(self):
        walk_in = Sale.objects.create(total_amount=Decimal("10"))
        load = self.new_load()
        with self.assertRaisesMessage(DistributionError, "no customer"):
            add_invoices(load, [walk_in.id])
        sale = self.invoiced(self.shop, (self.biscuit, 1, PACK_PIECE))
        add_invoices(load, [sale.id])
        dispatch_load(load)
        later = self.invoiced(self.shop, (self.biscuit, 1, PACK_PIECE))
        with self.assertRaisesMessage(DistributionError, "has left"):
            add_invoices(load, [later.id])
        # Taking an invoice off is refused too.
        response = self.client.post(reverse("distribution_load_remove_invoice", args=[load.id, sale.id]), follow=True)
        self.assertTrue(load.invoices.filter(sale=sale).exists())
        self.assertTrue(any("has left" in text for text in self.messages_of(response)))

    def test_an_empty_load_cannot_leave(self):
        load = self.new_load()
        with self.assertRaisesMessage(DistributionError, "at least one invoice"):
            dispatch_load(load)

    def test_invoice_is_taken_off_while_loading(self):
        sale = self.invoiced(self.shop, (self.biscuit, 1, PACK_PIECE))
        load = self.new_load()
        add_invoices(load, [sale.id])
        self.client.post(reverse("distribution_load_remove_invoice", args=[load.id, sale.id]))
        self.assertFalse(load.invoices.exists())


class LoadSheetTests(LoadCase):
    def test_sheet_totals_products_in_cartons_and_loose_pieces(self):
        # Soap: 2 cartons on one bill, 3 boxes and 5 pieces on another -> 2 Ctn, 3 Box, 5 Pcs.
        first = self.invoiced(self.shop, (self.soap, 2, PACK_CARTON), (self.biscuit, 7, PACK_PIECE))
        second = self.invoiced(self.shop, (self.soap, 3, PACK_BOX), (self.soap, 5, PACK_PIECE))
        load = self.new_load()
        add_invoices(load, [first.id, second.id])

        sheet = load_sheet(load)

        soap = next(line for line in sheet["lines"] if line["product"] == self.soap)
        self.assertEqual((soap["cartons"], soap["boxes"], soap["pieces"], soap["packing"]), (2, 3, 5, "10x12"))
        biscuit = next(line for line in sheet["lines"] if line["product"] == self.biscuit)
        self.assertEqual((biscuit["cartons"], biscuit["pieces"]), (0, 7))
        self.assertTrue(sheet["show_boxes"])
        self.assertEqual(sheet["total_cartons"], 2)
        self.assertEqual(sheet["invoice_count"], 2)
        self.assertEqual(sheet["bill_value"], first.total_amount + second.total_amount)
        self.assertEqual(sheet["total_amount"], sheet["bill_value"])

        page = self.client.get(reverse("distribution_load_print", args=[load.id]))
        self.assertEqual(page.status_code, 200)
        for text in ("Load sheet", "CTN", "BOX", "PCS", "Storekeeper", "Delivery man", first.display_number, "Ali Store", "Shehzore"):
            self.assertContains(page, text)

    def test_no_box_column_when_nothing_is_in_boxes(self):
        sale = self.invoiced(self.shop, (self.soap, 1, PACK_CARTON), (self.biscuit, 3, PACK_PIECE))
        load = self.new_load()
        add_invoices(load, [sale.id])
        self.assertFalse(load_sheet(load)["show_boxes"])
        page = self.client.get(reverse("distribution_load_print", args=[load.id]))
        self.assertNotContains(page, ">BOX<")

    def test_list_and_detail_pages_render(self):
        sale = self.invoiced(self.shop, (self.soap, 1, PACK_CARTON))
        load = self.new_load()
        add_invoices(load, [sale.id])
        listing = self.client.get(reverse("distribution_load_list"))
        self.assertContains(listing, load.display_number)
        detail = self.client.get(reverse("distribution_load_detail", args=[load.id]))
        self.assertContains(detail, sale.display_number)
        self.assertContains(detail, "Products to load")
