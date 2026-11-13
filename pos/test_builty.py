"""Builty (bilty): the transporter's freight bill, shared over a day's purchases and added to cost.

Covers the split to the paisa (per box, per carton, per piece, by value), how a product
without boxes is counted, the stock / cost of goods sold split for goods already gone, the
weighted-average cost it raises, the voucher, the transporter's payable and statement,
the bank receipt rule, the day screen, the purchase pages, reversal and the module switch.
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import ProtectedError, Sum
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import (
    AccountingSettings,
    BankAccount,
    PartyLedgerEntry,
    Voucher,
    VoucherLine,
)
from accounts.services.bank_receipts import MISSING_MESSAGE, get_receipt
from accounts.services.party_statement import get_party_statement
from accounts.services.pos_posting import post_purchase_from_pos
from pos.models import (
    PACK_BOX,
    PACK_CARTON,
    PACK_PIECE,
    Builty,
    BuiltyAllocation,
    Product,
    Purchase,
    PurchaseItem,
    StockAdjustment,
    Supplier,
    SystemSettings,
)
from pos.services import modules
from pos.services.builty import (
    freight_by_item,
    plan_split,
    post_builty,
    purchase_lines,
    reverse_builty,
    split_paisa,
)
from pos.services.stock_control import create_stock_adjustment
from pos.test_support import AuthenticatedAdminTestCase

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def money(value):
    return Decimal(value).quantize(Decimal("0.01"))


def account_balance(title):
    totals = VoucherLine.objects.filter(account__title=title).aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return (totals["debit"] or Decimal("0")) - (totals["credit"] or Decimal("0"))


class BuiltyCase(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(name="Lahore Traders")
        self.transporter = Supplier.objects.create(name="Daewoo Cargo")
        # 1 carton = 82 boxes = 1,968 pieces; a carton costs 19,680, so a piece costs exactly 10.
        self.biscuits = Product.objects.create(
            name="Biscuits", sku="BIS", cost_price=0, price=Decimal("15"), stock=0,
            pieces_per_box=24, boxes_per_carton=82,
        )
        # Cartons of 12 pieces and no boxes.
        self.oil = Product.objects.create(
            name="Oil", sku="OIL", cost_price=0, price=Decimal("600"), stock=0,
            pieces_per_box=1, boxes_per_carton=12,
        )
        # Boxes of 12 and no cartons.
        self.soap = Product.objects.create(
            name="Soap", sku="SOAP", cost_price=0, price=Decimal("80"), stock=0,
            pieces_per_box=12, boxes_per_carton=1,
        )
        # Plain pieces.
        self.broom = Product.objects.create(name="Broom", sku="BRM", cost_price=0, price=Decimal("300"), stock=0)

    # -- builders -----------------------------------------------------------------------

    def buy(self, *lines, supplier=None, paid_in_cash=True):
        """lines: (product, quantity, cost per unit, unit level)."""
        purchase = Purchase.objects.create(supplier=supplier or self.supplier, payment_method=Purchase.CASH)
        for product, quantity, cost, level in lines:
            item = PurchaseItem(
                purchase=purchase,
                product=Product.objects.get(pk=product.pk),
                quantity=Decimal(str(quantity)),
                cost_price=Decimal(str(cost)),
                sale_price=Decimal(str(cost)) * 2,
                unit_level=level,
            )
            item.save()
        purchase.refresh_from_db()
        if paid_in_cash:
            purchase.paid_amount = purchase.total_amount
            purchase.save(update_fields=["paid_amount"])
        post_purchase_from_pos(
            purchase_id=purchase.id,
            supplier=purchase.supplier,
            total_amount=purchase.total_amount,
            paid_amount=purchase.paid_amount,
            payment_method=purchase.payment_method,
            purchase_date=timezone.localdate(),
        )
        return purchase

    def reload(self, product):
        return Product.objects.get(pk=product.pk)


# --------------------------------------------------------------------------- the split

class SplitTests(BuiltyCase):
    def test_paisa_split_adds_up_exactly_and_gives_leftovers_to_the_largest_remainders(self):
        self.assertEqual(split_paisa(Decimal("1000"), [1, 1, 1]), [money("333.34"), money("333.33"), money("333.33")])
        sevenths = split_paisa(Decimal("100"), [1] * 7)
        self.assertEqual(sum(sevenths), Decimal("100.00"))
        self.assertEqual(sevenths[:4], [money("14.29")] * 4)
        self.assertEqual(sevenths[4:], [money("14.28")] * 3)
        # The remainder decides, not the position: 2/3 of a paisa beats 1/3.
        self.assertEqual(split_paisa(Decimal("0.05"), [1, 2]), [money("0.02"), money("0.03")])

    def test_rs_4100_over_410_boxes_is_ten_a_box(self):
        purchase = self.buy((self.biscuits, 5, "19680.00", PACK_CARTON))

        rows = plan_split(purchase_lines([purchase]), Decimal("4100"), Builty.PER_BOX)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].counted_as, PACK_BOX)
        self.assertEqual(rows[0].units, Decimal("410.000"))
        self.assertEqual(rows[0].amount, Decimal("4100.00"))

        builty = post_builty(purchases=[purchase], amount="4100", payment_method=Builty.CASH, actor=self.admin_user)
        allocation = builty.allocations.get()
        self.assertEqual(allocation.share_per_unit, Decimal("10.000000"))
        # A carton held 82 boxes: 820 of freight on each, so it landed at 19,680 + 820.
        self.assertEqual(allocation.landed_unit_cost, Decimal("20500.00"))

    def test_1000_over_three_lines_splits_to_the_paisa(self):
        purchase = self.buy(
            (self.broom, 1, "100.00", PACK_PIECE),
            (self.broom, 1, "100.00", PACK_PIECE),
            (self.broom, 1, "100.00", PACK_PIECE),
        )

        builty = post_builty(purchases=[purchase], amount="1000", payment_method=Builty.CASH)

        shares = list(builty.allocations.order_by("id").values_list("amount", flat=True))
        self.assertEqual(shares, [money("333.34"), money("333.33"), money("333.33")])
        self.assertEqual(sum(shares), Decimal("1000.00"))

    def test_products_without_boxes_count_cartons_and_plain_products_count_pieces(self):
        purchase = self.buy(
            (self.biscuits, 5, "19680.00", PACK_CARTON),  # 410 boxes
            (self.oil, 10, "6000.00", PACK_CARTON),  # no boxes: 10 cartons
            (self.broom, 25, "200.00", PACK_PIECE),  # neither: 25 pieces
        )

        rows = plan_split(purchase_lines([purchase]), Decimal("4450"), Builty.PER_BOX)

        self.assertEqual([row.counted_as for row in rows], [PACK_BOX, PACK_CARTON, PACK_PIECE])
        self.assertEqual([row.counted_text for row in rows], ["410 Box", "10 Ctn", "25 pcs"])
        self.assertEqual([row.amount for row in rows], [money("4100"), money("100"), money("250")])

    def test_per_carton_falls_back_to_boxes_and_per_piece_counts_pieces(self):
        purchase = self.buy(
            (self.biscuits, 1, "19680.00", PACK_CARTON),  # 1 carton, 1,968 pieces
            (self.soap, 3, "600.00", PACK_BOX),  # no cartons: 3 boxes, 36 pieces
        )
        items = purchase_lines([purchase])

        per_carton = plan_split(items, Decimal("400"), Builty.PER_CARTON)
        per_piece = plan_split(items, Decimal("2004"), Builty.PER_PIECE)

        self.assertEqual([row.counted_text for row in per_carton], ["1 Ctn", "3 Box"])
        self.assertEqual([row.amount for row in per_carton], [money("100"), money("300")])
        self.assertEqual([row.counted_text for row in per_piece], ["1968 pcs", "36 pcs"])
        self.assertEqual([row.amount for row in per_piece], [money("1968"), money("36")])

    def test_by_value_follows_the_line_amounts(self):
        purchase = self.buy((self.broom, 10, "300.00", PACK_PIECE), (self.soap, 1, "1000.00", PACK_BOX))

        rows = plan_split(purchase_lines([purchase]), Decimal("1000"), Builty.BY_VALUE)

        self.assertEqual([row.amount for row in rows], [money("750"), money("250")])

    def test_cartons_and_boxes_switched_off_counts_everything_in_pieces(self):
        purchase = self.buy((self.broom, 4, "100.00", PACK_PIECE))
        modules.set_module_switch("packaging", False)
        try:
            rows = plan_split(purchase_lines([purchase]), Decimal("40"), Builty.PER_BOX)
        finally:
            modules.set_module_switch("packaging", True)
        self.assertEqual(rows[0].counted_as, PACK_PIECE)
        self.assertEqual(rows[0].amount, money("40"))


# ------------------------------------------------------------------- cost and posting

class CostAndPostingTests(BuiltyCase):
    def test_goods_all_on_the_shelf_raise_the_average_cost_and_inventory(self):
        purchase = self.buy((self.biscuits, 5, "19680.00", PACK_CARTON))
        self.assertEqual(self.reload(self.biscuits).cost_price, Decimal("10.000000"))

        builty = post_builty(purchases=[purchase], amount="4100", payment_method=Builty.CASH)

        biscuits = self.reload(self.biscuits)
        # 10 + 4,100 / 9,840 pieces, at six places.
        self.assertEqual(biscuits.cost_price, Decimal("10.416667"))
        self.assertEqual(builty.stock_amount, Decimal("4100.00"))
        self.assertEqual(builty.cogs_amount, Decimal("0.00"))
        self.assertEqual(account_balance("Inventory"), Decimal("98400.00") + Decimal("4100.00"))

    def test_earlier_stock_shares_the_raise_by_weighted_average(self):
        self.buy((self.broom, 50, "8.00", PACK_PIECE))
        purchase = self.buy((self.broom, 50, "12.00", PACK_PIECE))
        self.assertEqual(self.reload(self.broom).cost_price, Decimal("10.00"))

        post_builty(purchases=[purchase], amount="100", basis=Builty.PER_PIECE, payment_method=Builty.CASH)

        # (100 pieces x 10 + 100) / 100 pieces.
        self.assertEqual(self.reload(self.broom).cost_price, Decimal("11.000000"))

    def test_goods_already_sold_send_their_share_to_cost_of_goods_sold(self):
        purchase = self.buy((self.broom, 100, "10.00", PACK_PIECE))
        create_stock_adjustment(
            product=self.broom, adjustment_mode=StockAdjustment.ADJUSTMENT, quantity_delta=-40, reason="Sold", actor=self.admin_user
        )

        builty = post_builty(purchases=[purchase], amount="200", basis=Builty.PER_PIECE, payment_method=Builty.CASH)

        allocation = builty.allocations.get()
        self.assertEqual(allocation.amount_to_stock, Decimal("120.00"))
        self.assertEqual(allocation.amount_to_cogs, Decimal("80.00"))
        self.assertEqual(allocation.stock_at_posting, Decimal("60.000"))
        broom = self.reload(self.broom)
        self.assertEqual(broom.cost_price, Decimal("12.000000"))
        # The books agree with the shelf: 60 pieces at 12.
        self.assertEqual(account_balance("Inventory"), Decimal("720.00"))
        self.assertEqual(account_balance("Cost of Goods Sold"), Decimal("80.00"))

    def test_everything_sold_puts_the_whole_freight_on_cost_of_goods_sold(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        create_stock_adjustment(product=self.broom, adjustment_mode=StockAdjustment.ADJUSTMENT, quantity_delta=-10, reason="Sold")

        builty = post_builty(purchases=[purchase], amount="50", payment_method=Builty.CASH)

        self.assertEqual((builty.stock_amount, builty.cogs_amount), (Decimal("0.00"), Decimal("50.00")))
        self.assertEqual(self.reload(self.broom).cost_price, Decimal("10.00"))

    def test_voucher_is_balanced_and_points_back_at_the_builty(self):
        purchase = self.buy((self.broom, 100, "10.00", PACK_PIECE))
        create_stock_adjustment(product=self.broom, adjustment_mode=StockAdjustment.ADJUSTMENT, quantity_delta=-25, reason="Sold")

        builty = post_builty(purchases=[purchase], amount="333.33", payment_method=Builty.CASH, builty_number="GR-9")

        voucher = builty.voucher
        self.assertEqual((voucher.ref_model, voucher.ref_id, voucher.voucher_type), ("Builty", builty.id, Voucher.PURCHASE))
        lines = {line.account.title: (line.debit, line.credit) for line in voucher.lines.select_related("account")}
        self.assertEqual(lines["Inventory"], (Decimal("250.00"), Decimal("0")))
        self.assertEqual(lines["Cost of Goods Sold"], (Decimal("83.33"), Decimal("0")))
        self.assertEqual(lines["Cash"], (Decimal("0"), Decimal("333.33")))
        self.assertEqual(voucher.total_debit, voucher.total_credit)
        self.assertIn("Builty GR-9", voucher.narration)

    def test_paid_from_a_bank_ledger(self):
        bank = BankAccount.objects.create(name="Meezan", account_number="0101")
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))

        builty = post_builty(purchases=[purchase], amount="100", payment_method=Builty.BANK, bank_account=bank)

        credit = builty.voucher.lines.get(credit__gt=0)
        self.assertEqual(credit.account_id, bank.account_id)

    def test_transporter_is_owed_and_sees_the_builty_on_his_statement(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))

        builty = post_builty(
            purchases=[purchase], amount="1000", payment_method=Builty.CREDIT, transporter=self.transporter, builty_number="GR-77"
        )

        self.assertEqual(account_balance("Accounts Payable"), Decimal("-1000.00"))
        entry = PartyLedgerEntry.objects.get(voucher=builty.voucher)
        self.assertEqual((entry.object_id, entry.credit), (self.transporter.id, Decimal("1000.00")))
        statement = get_party_statement(party=self.transporter)
        self.assertEqual(statement["closing_balance"], Decimal("-1000.00"))
        self.assertEqual(statement["balance_type"], "payable")
        self.assertEqual(statement["rows"][0]["document"], f"Builty #{builty.id} (no. GR-77)")
        self.assertEqual(self.transporter.raw_balance, Decimal("-1000.00"))

    def test_on_account_needs_a_transporter_and_a_builty_is_never_future_dated(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        with self.assertRaisesMessage(ValueError, "Choose the transporter"):
            post_builty(purchases=[purchase], amount="100", payment_method=Builty.CREDIT)
        with self.assertRaisesMessage(ValueError, "future"):
            post_builty(purchases=[purchase], amount="100", date=timezone.localdate() + timedelta(days=1))
        with self.assertRaisesMessage(ValueError, "freight amount"):
            post_builty(purchases=[purchase], amount="0")
        self.assertFalse(Builty.objects.exists())

    def test_locked_books_refuse_a_builty(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        AccountingSettings.objects.update_or_create(pk=1, defaults={"books_locked_until": timezone.localdate()})

        with self.assertRaisesMessage(ValueError, "books are locked"):
            post_builty(purchases=[purchase], amount="100")
        self.assertFalse(Builty.objects.exists())
        self.assertEqual(self.reload(self.broom).cost_price, Decimal("10.00"))


# -------------------------------------------------------------------------- reversal

class ReversalTests(BuiltyCase):
    def test_reversal_takes_back_the_freight_still_on_the_shelf_and_the_rest_off_cogs(self):
        purchase = self.buy((self.broom, 100, "10.00", PACK_PIECE))
        create_stock_adjustment(product=self.broom, adjustment_mode=StockAdjustment.ADJUSTMENT, quantity_delta=-40, reason="Sold")
        builty = post_builty(purchases=[purchase], amount="200", payment_method=Builty.CASH)  # 120 to stock, 2 a piece
        create_stock_adjustment(product=self.broom, adjustment_mode=StockAdjustment.ADJUSTMENT, quantity_delta=-30, reason="Sold")

        builty = reverse_builty(builty, reason="Wrong amount", actor=self.admin_user)

        # 30 pieces left x the 2 a piece the builty added.
        self.assertEqual(builty.reversal_stock_amount, Decimal("60.00"))
        self.assertEqual(builty.reversal_cogs_amount, Decimal("140.00"))
        self.assertEqual(self.reload(self.broom).cost_price, Decimal("10.000000"))
        voucher = builty.reversal_voucher
        self.assertEqual((voucher.ref_model, voucher.ref_id), ("BuiltyReversal", builty.id))
        self.assertEqual(voucher.total_debit, voucher.total_credit)
        self.assertEqual(account_balance("Cash"), Decimal("-1000.00"))  # the purchase; the builty came back
        self.assertEqual(account_balance("Inventory"), Decimal("300.00"))  # 30 pieces at 10
        self.assertEqual(freight_by_item([purchase.items.get().id]), {})

        with self.assertRaisesMessage(ValueError, "already reversed"):
            reverse_builty(builty, reason="Again")

    def test_reversing_an_unpaid_builty_clears_the_transporter(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        builty = post_builty(purchases=[purchase], amount="500", payment_method=Builty.CREDIT, transporter=self.transporter)

        reverse_builty(builty, reason="Charged to the supplier instead")

        self.assertEqual(self.transporter.raw_balance, Decimal("0"))
        documents = [row["document"] for row in get_party_statement(party=self.transporter)["rows"]]
        self.assertEqual(documents, [f"Builty #{builty.id}", f"Builty #{builty.id} reversed"])
        self.assertEqual(self.reload(self.broom).cost_price, Decimal("10.000000"))

    def test_reverse_page_needs_a_reason(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        builty = post_builty(purchases=[purchase], amount="50")

        refused = self.client.post(reverse("builty_reverse", args=[builty.id]), {"reason": ""})
        builty.refresh_from_db()
        self.assertFalse(builty.is_reversed)
        self.assertRedirects(refused, reverse("builty_detail", args=[builty.id]), fetch_redirect_response=False)

        self.client.post(reverse("builty_reverse", args=[builty.id]), {"reason": "Duplicate"})
        builty.refresh_from_db()
        self.assertTrue(builty.is_reversed)
        self.assertEqual(builty.reversed_by, self.admin_user)


# -------------------------------------------------------------------------- screens

class ScreenTests(BuiltyCase):
    def test_day_screen_lists_that_days_purchases_all_ticked(self):
        today = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        yesterday = self.buy((self.soap, 2, "600.00", PACK_BOX))
        Purchase.objects.filter(pk=yesterday.pk).update(created_at=timezone.now() - timedelta(days=1))
        other_supplier = self.buy((self.broom, 1, "10.00", PACK_PIECE), supplier=Supplier.objects.create(name="Other"))

        page = self.client.get(reverse("builty_add"))
        listed = {purchase.id: purchase.is_ticked for purchase in page.context["purchases"]}
        self.assertEqual(listed, {today.id: True, other_supplier.id: True})

        day = (timezone.localdate() - timedelta(days=1)).isoformat()
        earlier = self.client.get(reverse("builty_add"), {"day": day})
        self.assertEqual([purchase.id for purchase in earlier.context["purchases"]], [yesterday.id])

        filtered = self.client.get(reverse("builty_add"), {"supplier": self.supplier.id})
        self.assertEqual([purchase.id for purchase in filtered.context["purchases"]], [today.id])
        self.assertContains(page, 'id="builtyPreview"')

    def test_from_a_purchase_only_that_purchase_starts_ticked(self):
        first = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        second = self.buy((self.broom, 5, "10.00", PACK_PIECE))

        page = self.client.get(reverse("builty_add"), {"purchase": second.id})

        ticked = {purchase.id: purchase.is_ticked for purchase in page.context["purchases"]}
        self.assertEqual(ticked, {first.id: False, second.id: True})
        self.assertEqual(page.context["back_url"], reverse("purchase_detail", args=[second.id]))

    def test_saving_from_the_day_screen_posts_over_the_ticked_purchases(self):
        first = self.buy((self.biscuits, 5, "19680.00", PACK_CARTON))
        second = self.buy((self.broom, 10, "10.00", PACK_PIECE))

        response = self.client.post(
            reverse("builty_add"),
            {
                "day": timezone.localdate().isoformat(),
                "purchases": [first.id],
                "date": timezone.localdate().isoformat(),
                "builty_number": "GR-5",
                "amount": "4100",
                "basis": Builty.PER_BOX,
                "payment_method": Builty.CREDIT,
                "transporter": self.transporter.id,
                "notes": "",
            },
        )

        builty = Builty.objects.get()
        self.assertRedirects(response, reverse("builty_detail", args=[builty.id]), fetch_redirect_response=False)
        self.assertEqual(list(builty.purchases.all()), [first])
        self.assertEqual(builty.created_by, self.admin_user)
        self.assertFalse(BuiltyAllocation.objects.filter(purchase_item__purchase=second).exists())

        detail = self.client.get(reverse("builty_detail", args=[builty.id]))
        self.assertContains(detail, "410 Box")
        self.assertContains(detail, "Daewoo Cargo")

    def test_nothing_ticked_and_unpaid_without_transporter_are_refused(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        payload = {
            "day": timezone.localdate().isoformat(),
            "date": timezone.localdate().isoformat(),
            "amount": "100",
            "basis": Builty.PER_BOX,
            "payment_method": Builty.CASH,
        }

        nothing = self.client.post(reverse("builty_add"), payload)
        self.assertContains(nothing, "Tick at least one purchase")

        unpaid = self.client.post(reverse("builty_add"), {**payload, "purchases": [purchase.id], "payment_method": Builty.CREDIT})
        self.assertIn("transporter", unpaid.context["form"].errors)
        self.assertFalse(Builty.objects.exists())

    def test_purchase_pages_show_the_builty_freight_and_landed_cost(self):
        purchase = self.buy((self.biscuits, 5, "19680.00", PACK_CARTON))
        detail = self.client.get(reverse("purchase_detail", args=[purchase.id]))
        self.assertContains(detail, f"{reverse('builty_add')}?purchase={purchase.id}")

        builty = post_builty(purchases=[purchase], amount="4100", payment_method=Builty.CASH, builty_number="GR-1")

        detail = self.client.get(reverse("purchase_detail", args=[purchase.id]))
        self.assertTrue(detail.context["has_freight"])
        self.assertEqual(detail.context["freight_total"], Decimal("4100.00"))
        item = detail.context["items"][0]
        self.assertEqual(item.landed_unit_cost, Decimal("20500.00"))
        self.assertEqual(item.landed_piece_cost, Decimal("10.416667"))
        self.assertContains(detail, "Landed cost")
        self.assertContains(detail, reverse("builty_detail", args=[builty.id]))

        listing = self.client.get(reverse("purchase_list"))
        self.assertContains(listing, "Builty")
        row = next(p for p in listing.context["purchases"] if p.id == purchase.id)
        self.assertEqual(row.freight_total, Decimal("4100.00"))

        builties = self.client.get(reverse("builty_list"))
        self.assertContains(builties, "Builty GR-1")

    def test_a_purchase_line_under_a_builty_cannot_be_removed(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE), (self.soap, 1, "600.00", PACK_BOX))
        post_builty(purchases=[purchase], amount="100")
        line = purchase.items.get(product=self.broom)
        with self.assertRaises(ProtectedError):
            line.delete()
        self.assertTrue(PurchaseItem.objects.filter(pk=line.pk).exists())


# ----------------------------------------------------------------- bank receipt rule

class BankReceiptTests(BuiltyCase):
    @classmethod
    def setUpClass(cls):
        cls._media_root = tempfile.mkdtemp(prefix="zehnms-builty-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_root)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)

    def setUp(self):
        super().setUp()
        SystemSettings.load_raw()
        SystemSettings.objects.filter(pk=1).update(require_bank_receipt=True)
        self.bank = BankAccount.objects.create(name="Meezan", account_number="0101")
        self.purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))

    def payload(self, **extra):
        data = {
            "day": timezone.localdate().isoformat(),
            "purchases": [self.purchase.id],
            "date": timezone.localdate().isoformat(),
            "amount": "100",
            "basis": Builty.PER_BOX,
            "payment_method": Builty.BANK,
            "bank_account": self.bank.id,
        }
        data.update(extra)
        return data

    def test_bank_paid_builty_needs_its_receipt(self):
        response = self.client.post(reverse("builty_add"), self.payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors["bank_receipt"], [MISSING_MESSAGE])
        self.assertFalse(Builty.objects.exists())

    def test_bank_paid_builty_is_saved_with_its_receipt(self):
        receipt = SimpleUploadedFile("slip.pdf", PDF_BYTES, content_type="application/pdf")

        response = self.client.post(reverse("builty_add"), self.payload(bank_receipt=receipt))

        builty = Builty.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_receipt(builty).mime_type, "application/pdf")
        page = self.client.get(reverse("bank_receipt", args=["builty", builty.id]))
        self.assertEqual(page.status_code, 200)

    def test_bank_needs_a_ledger_and_cash_needs_no_receipt(self):
        no_ledger = self.client.post(reverse("builty_add"), self.payload(bank_account=""))
        self.assertIn("bank_account", no_ledger.context["form"].errors)

        cash = self.client.post(reverse("builty_add"), self.payload(payment_method=Builty.CASH, bank_account=""))
        self.assertEqual(cash.status_code, 302)


# ------------------------------------------------------------------------- the module

class ModuleTests(BuiltyCase):
    def test_module_off_hides_the_screens_and_the_button(self):
        purchase = self.buy((self.broom, 10, "10.00", PACK_PIECE))
        modules.set_module_switch("builty", False)

        self.assertRedirects(self.client.get(reverse("builty_list")), reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("builty_add")).status_code, 302)
        detail = self.client.get(reverse("purchase_detail", args=[purchase.id]))
        self.assertNotContains(detail, reverse("builty_add"))
        listing = self.client.get(reverse("purchase_list"))
        self.assertNotContains(listing, reverse("builty_list"))

    def test_module_is_on_by_default_and_in_the_standard_edition(self):
        self.assertTrue(modules.module_enabled("builty"))
        self.assertIn("builty", modules.edition_modules("standard"))
