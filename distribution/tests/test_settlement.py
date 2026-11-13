"""Day-end settlement of a load: goods that came back, money recovered on the run, the cash
handed in, and the books after it."""
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from accounts.models import Settlement, Voucher
from pos.models import SaleReturn

from distribution.models import LoadForm, Recovery
from distribution.services.common import DistributionError
from distribution.services.loads import (
    CASH_SHORT_OVER,
    add_invoices,
    close_load,
    create_load,
    dispatch_load,
    post_load_returns,
    reopen_load,
    settlement_figures,
)
from distribution.services.orders import invoice_order
from distribution.services.recovery import record_recovery

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, DistributionCase

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


class SettlementCase(DistributionCase):
    def setUp(self):
        super().setUp()
        # An old balance the shop owes from before.
        old = invoice_order(self.book(self.shop, (self.biscuit, 10, PACK_PIECE)), user=self.admin_user)
        self.old_sale = old.sale  # 300 on account
        self.credit_bill = invoice_order(self.book(self.shop, (self.soap, 1, PACK_CARTON)), user=self.admin_user).sale  # 1,440
        self.cash_bill = invoice_order(self.book(self.other_shop, (self.biscuit, 10, PACK_PIECE)), user=self.admin_user, credit=False).sale  # 300 cash
        self.load = create_load(load_date=self.today, route=self.route, delivery_man=self.driver, vehicle=self.van, user=self.admin_user)
        add_invoices(self.load, [self.credit_bill.id, self.cash_bill.id])
        dispatch_load(self.load)

    def item(self, sale, product):
        return sale.items.get(product=product)


class ReturnsAtSettlementTests(SettlementCase):
    def test_goods_back_on_a_credit_bill_reduce_the_account_and_go_back_on_the_shelf(self):
        soap_line = self.item(self.credit_bill, self.soap)
        self.refresh(self.soap)
        stock_before = self.soap.stock
        response = self.client.post(
            reverse("distribution_load_settle", args=[self.load.id]),
            {"action": "returns", "reason": "Shop short of cash", f"ret_{soap_line.id}": "3", f"unit_{soap_line.id}": PACK_BOX},
            follow=True,
        )
        self.assertTrue(any("1 return posted" in text for text in self.messages_of(response)), self.messages_of(response))

        sale_return = SaleReturn.objects.get()
        self.assertEqual((sale_return.refund_method, sale_return.total_amount), (SaleReturn.RECEIVABLE, Decimal("432.00")))
        self.assertTrue(self.load.sale_returns.filter(sale_return=sale_return).exists())
        self.refresh(self.soap)
        self.assertEqual(self.soap.stock, stock_before + 36)
        self.assertEqual(self.balance(self.shop), Decimal("300.00") + Decimal("1440.00") - Decimal("432.00"))
        self.assert_books_balance()

    def test_goods_back_on_a_cash_bill_come_off_the_cash_expected(self):
        line = self.item(self.cash_bill, self.biscuit)
        post_load_returns(self.load, {self.cash_bill.id: {"quantities": {line.id: Decimal("4")}}}, reason="Refused", user=self.admin_user)

        figures = settlement_figures(self.load)

        self.assertEqual(SaleReturn.objects.get().refund_method, SaleReturn.CASH)
        self.assertEqual((figures["cash_bills"], figures["cash_refunds"], figures["cash_expected"]), (Decimal("300.00"), Decimal("120.00"), Decimal("180.00")))
        self.assertEqual(figures["returns_total"], Decimal("120.00"))
        self.assertEqual(figures["delivered"], Decimal("1620.00"))

    def test_a_return_needs_a_reason_and_cannot_exceed_the_invoice(self):
        line = self.item(self.credit_bill, self.soap)
        with self.assertRaisesMessage(DistributionError, "Say why"):
            post_load_returns(self.load, {self.credit_bill.id: {"quantities": {line.id: 1}}}, reason="")
        with self.assertRaisesMessage(DistributionError, self.credit_bill.display_number):
            post_load_returns(self.load, {self.credit_bill.id: {"quantities": {line.id: 2}}}, reason="Refused")
        self.assertFalse(SaleReturn.objects.exists())

    def test_invoices_not_on_the_load_are_refused(self):
        line = self.item(self.old_sale, self.biscuit)
        with self.assertRaisesMessage(DistributionError, "not on this load"):
            post_load_returns(self.load, {self.old_sale.id: {"quantities": {line.id: 1}}}, reason="x")


class RecoveryAndCloseTests(SettlementCase):
    def test_full_day_end_balances_the_books(self):
        # 2 boxes of soap came back; the shop paid 500 of its old balance in cash; the cash bill was paid.
        soap_line = self.item(self.credit_bill, self.soap)
        post_load_returns(
            self.load, {self.credit_bill.id: {"quantities": {soap_line.id: 2}, "units": {soap_line.id: PACK_BOX}}},
            reason="Refused", user=self.admin_user,
        )
        self.client.post(
            reverse("distribution_load_settle", args=[self.load.id]),
            {"action": "recovery", "customer": self.shop.id, "amount": "500", "payment_method": "cash", "sale": self.credit_bill.id},
        )
        recovery = Recovery.objects.get()
        self.assertEqual((recovery.load_form, recovery.delivery_man, recovery.route, recovery.order_booker), (self.load, self.driver, self.route, self.booker))
        self.assertEqual((recovery.collected_by, recovery.collector_name, recovery.sale), (Recovery.BY_DELIVERY_MAN, "Aslam", self.credit_bill))
        self.assertEqual(recovery.settlement.settlement_type, Settlement.RECEIPT)

        figures = settlement_figures(self.load)
        self.assertEqual(figures["cash_expected"], Decimal("800.00"))  # 300 cash bill + 500 recovered
        cash_before = self.account_balance("Cash")

        # The delivery man hands in 780: 20 short.
        response = self.client.post(
            reverse("distribution_load_settle", args=[self.load.id]), {"action": "close", "cash_received": "780"}, follow=True
        )
        self.assertTrue(any("short by 20.00" in text for text in self.messages_of(response)), self.messages_of(response))

        self.refresh(self.load)
        self.assertEqual(self.load.status, LoadForm.SETTLED)
        self.assertEqual((self.load.cash_expected, self.load.cash_received, self.load.cash_variance), (Decimal("800.00"), Decimal("780.00"), Decimal("-20.00")))
        voucher = self.load.variance_voucher
        self.assertEqual(voucher.ref_model, "LoadForm")
        self.assertEqual(self.account_balance(CASH_SHORT_OVER), Decimal("20.00"))
        self.assertEqual(self.account_balance("Cash"), cash_before - Decimal("20.00"))
        # Cash in the books is now exactly what was handed in for this run.
        self.assertEqual(self.account_balance("Cash"), Decimal("780.00"))
        # The shop: 300 old + 1,440 - 288 back - 500 paid.
        self.assertEqual(self.balance(self.shop), Decimal("952.00"))
        self.assertEqual(self.account_balance("Accounts Receivable"), Decimal("952.00"))
        self.assert_books_balance()

        # Nothing more is recorded on a settled load.
        with self.assertRaises(DistributionError):
            close_load(self.load, cash_received=Decimal("780"))
        with self.assertRaises(DistributionError):
            post_load_returns(self.load, {self.credit_bill.id: {"quantities": {soap_line.id: 1}}}, reason="x")
        page = self.client.get(reverse("distribution_load_settle", args=[self.load.id]))
        self.assertContains(page, "Short")

    def test_cash_over_and_exact_cash(self):
        close_load(self.load, cash_received=Decimal("310"), user=self.admin_user)
        self.refresh(self.load)
        self.assertEqual(self.load.cash_variance, Decimal("10.00"))
        self.assertEqual(self.account_balance(CASH_SHORT_OVER), Decimal("-10.00"))
        self.assert_books_balance()

        load = create_load(load_date=self.today, user=self.admin_user)
        sale = invoice_order(self.book(self.shop, (self.biscuit, 1, PACK_PIECE)), user=self.admin_user).sale
        add_invoices(load, [sale.id])
        dispatch_load(load)
        close_load(load, cash_received=Decimal("0"))
        self.refresh(load)
        self.assertEqual((load.cash_variance, load.variance_voucher), (Decimal("0.00"), None))

    def test_back_to_loading_only_before_anything_is_settled(self):
        reopen_load(self.load)
        self.refresh(self.load)
        self.assertEqual(self.load.status, LoadForm.OPEN)
        dispatch_load(self.load)
        record_recovery(customer=self.shop, amount=Decimal("50"), load_form=self.load, collected_by=Recovery.BY_DELIVERY_MAN, delivery_man=self.driver)
        with self.assertRaisesMessage(DistributionError, "stays out for delivery"):
            reopen_load(self.load)

    def test_bank_recovery_follows_the_bank_receipt_rule(self):
        from accounts.models import BankAccount

        self.settings_row(require_bank_receipt=True)
        bank = BankAccount.objects.create(name="Meezan", account_number="0101")
        url = reverse("distribution_load_settle", args=[self.load.id])
        refused = self.client.post(url, {"action": "recovery", "customer": self.shop.id, "amount": "200", "payment_method": "bank", "bank_account": bank.id}, follow=True)
        self.assertFalse(Recovery.objects.exists())
        self.assertTrue(any("bank receipt" in text.lower() for text in self.messages_of(refused)))

        self.client.post(
            url,
            {"action": "recovery", "customer": self.shop.id, "amount": "200", "payment_method": "bank", "bank_account": bank.id,
             "bank_receipt": SimpleUploadedFile("slip.pdf", PDF, content_type="application/pdf")},
        )
        recovery = Recovery.objects.get()
        self.assertEqual(recovery.payment_method, Recovery.BANK)
        self.assertEqual(recovery.settlement.bank_account, bank)
        figures = settlement_figures(self.load)
        # A bank recovery is not cash the delivery man owes.
        self.assertEqual((figures["bank_recovered"], figures["cash_expected"]), (Decimal("200.00"), Decimal("300.00")))
        self.assertTrue(Voucher.objects.filter(ref_model="Settlement", ref_id=recovery.settlement_id).exists())
