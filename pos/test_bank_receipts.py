"""Bank receipts: every bank payment carries its proof (a JPG, PNG, WEBP or PDF).

Covers the rule at each entry point (till, purchase, party settlement, expense), the optional
receipt on refunds and money desk entries, what a receipt file may be, who may see, add,
replace or remove one, the audit trail, and that the file has no public address.
"""
import io
import json
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import Client, RequestFactory, override_settings
from django.urls import resolve, reverse
from django.utils import timezone
from PIL import Image

from accounts.models import (
    AuditLog,
    BankAccount,
    BankReceipt,
    CapitalEntry,
    Expense,
    ExpenseCategory,
    ExpensePayment,
    MoneyTransfer,
    Settlement,
)
from accounts.receipt_views import serve_public_media
from accounts.services import bank_receipts
from accounts.services.bank_receipts import (
    MISSING_MESSAGE,
    attach_receipt,
    check_upload,
    get_receipt,
    receipt_marks,
    remove_receipt,
)
from accounts.services.expense_posting import post_expense
from accounts.services.settlements import create_receipt
from pos.models import (
    Customer,
    CustomRole,
    Product,
    Purchase,
    PurchaseItem,
    PurchaseReturn,
    Sale,
    SaleItem,
    SaleReturn,
    Supplier,
    SystemSettings,
)
from pos.services.access import CASHIER_ROLE, assign_role
from pos.test_support import AuthenticatedAdminTestCase

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def image_bytes(fmt="PNG", size=(40, 60)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (37, 99, 235)).save(buffer, format=fmt)
    return buffer.getvalue()


def png(name="transfer.png"):
    return SimpleUploadedFile(name, image_bytes("PNG"), content_type="image/png")


def jpg(name="photo.jpg"):
    return SimpleUploadedFile(name, image_bytes("JPEG"), content_type="image/jpeg")


def webp(name="shot.webp"):
    return SimpleUploadedFile(name, image_bytes("WEBP"), content_type="image/webp")


def pdf(name="bank-slip.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


def set_rule(required):
    SystemSettings.load_raw()
    SystemSettings.objects.filter(pk=1).update(require_bank_receipt=required)


class ReceiptCase(AuthenticatedAdminTestCase):
    """Receipt files go to a throwaway media folder, never to the project's own."""

    @classmethod
    def setUpClass(cls):
        cls._media_root = tempfile.mkdtemp(prefix="zehnms-receipts-")
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
        self.soap = Product.objects.create(
            name="Soap", sku="SOAP", cost_price=Decimal("40.00"), price=Decimal("100.00"), stock=20
        )

    # -- small builders -------------------------------------------------------------

    def user_with_role(self, username, role):
        user = get_user_model().objects.create_user(username=username, password="RolePass123!x")
        assign_role(user, role)
        return user

    def custom_role_user(self, username, **levels):
        permissions = {
            key: "none"
            for key in ("dashboard", "pos", "products", "stock_control", "purchases", "expenses", "parties", "accounts", "reports", "settings")
        }
        permissions.update(levels)
        role = CustomRole.objects.create(name=f"Role {username}", permissions=permissions)
        return self.user_with_role(username, f"custom:{role.slug}")

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def bank_sale(self, sold_by=None, customer=None):
        sale = Sale.objects.create(payment_method="bank", sold_by=sold_by or self.admin_user, customer=customer)
        SaleItem.objects.create(sale=sale, product=self.soap, quantity=1, price=self.soap.price, cost_price=self.soap.cost_price)
        sale.refresh_from_db()
        sale.paid_amount = sale.total_amount
        sale.save(update_fields=["paid_amount"])
        return sale

    def bank_purchase(self, paid="50.00"):
        supplier = Supplier.objects.create(name="Karachi Traders")
        purchase = Purchase.objects.create(supplier=supplier, payment_method="bank", paid_amount=Decimal("0"))
        PurchaseItem.objects.create(
            purchase=purchase, product=self.soap, quantity=5, cost_price=Decimal("40.00"), sale_price=Decimal("100.00")
        )
        purchase.refresh_from_db()
        Purchase.objects.filter(pk=purchase.pk).update(paid_amount=Decimal(paid))
        purchase.refresh_from_db()
        return purchase

    def receipt_page(self, record):
        return reverse("bank_receipt", args=[bank_receipts.kind_of(record).slug, record.pk])

    def receipt_file(self, record):
        return reverse("bank_receipt_file", args=[bank_receipts.kind_of(record).slug, record.pk])

    def body(self, response):
        return b"".join(response.streaming_content)


# ---------------------------------------------------------------------------- the file

class ReceiptFileRulesTests(ReceiptCase):
    def test_the_four_kinds_are_recognised_by_their_content(self):
        for upload, mime_type, extension in (
            (jpg(), "image/jpeg", ".jpg"),
            (png(), "image/png", ".png"),
            (webp(), "image/webp", ".webp"),
            (pdf(), "application/pdf", ".pdf"),
        ):
            with self.subTest(mime_type=mime_type):
                checked = check_upload(upload)
                self.assertEqual((checked.mime_type, checked.extension), (mime_type, extension))
                self.assertEqual(len(checked.sha256), 64)

    def test_the_name_and_the_claimed_type_do_not_decide(self):
        disguised = SimpleUploadedFile("statement.pdf", image_bytes("PNG"), content_type="application/pdf")

        checked = check_upload(disguised)

        self.assertEqual(checked.mime_type, "image/png")
        self.assertEqual(checked.extension, ".png")

    def test_other_content_is_refused_whatever_it_is_called(self):
        for name, content, content_type in (
            ("receipt.jpg", b"just some text pretending to be a photo", "image/jpeg"),
            ("receipt.png", b"<html><script>alert(1)</script></html>", "image/png"),
            ("receipt.pdf", b"MZ\x90\x00\x03\x00\x00\x00 a program", "application/pdf"),
            ("receipt.svg", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "image/svg+xml"),
            ("receipt.gif", image_bytes("GIF"), "image/gif"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError) as caught:
                    check_upload(SimpleUploadedFile(name, content, content_type=content_type))
                self.assertIn("JPG, PNG or WEBP image, or a PDF", caught.exception.messages[0])

    def test_a_damaged_image_is_refused(self):
        broken = SimpleUploadedFile("photo.png", image_bytes("PNG")[:60], content_type="image/png")

        with self.assertRaises(ValidationError) as caught:
            check_upload(broken)

        self.assertIn("could not be read", caught.exception.messages[0])

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(ValidationError):
            check_upload(SimpleUploadedFile("nothing.png", b"", content_type="image/png"))

    def test_the_size_limit_comes_from_the_setting(self):
        self.assertEqual(bank_receipts.max_upload_bytes(), 5 * 1024 * 1024)
        with override_settings(MAX_RECEIPT_UPLOAD_BYTES=64):
            with self.assertRaises(ValidationError) as caught:
                check_upload(png())
        self.assertIn("too large", caught.exception.messages[0])

    def test_the_stored_name_is_random_and_keeps_only_the_detected_extension(self):
        sale = self.bank_sale()

        receipt = attach_receipt(sale, SimpleUploadedFile("../../evil name.PDF", image_bytes("JPEG")), actor=self.admin_user)

        self.assertRegex(receipt.file.name, r"^bank_receipts/\d{4}/\d{2}/[0-9a-f]{32}\.jpg$")
        self.assertEqual(receipt.original_name, "evil name.PDF")
        self.assertEqual(receipt.mime_type, "image/jpeg")
        self.assertTrue(Path(receipt.file.path).is_file())
        self.assertEqual(Path(self._media_root), Path(receipt.file.path).parents[3])


# ---------------------------------------------------------------------------- the till

class TillReceiptTests(ReceiptCase):
    def set_cart(self, quantity=2):
        session = self.client.session
        session["cart"] = {
            str(self.soap.id): {
                "name": self.soap.name,
                "price": str(self.soap.price),
                "original_price": str(self.soap.price),
                "discount_percent": "0.00",
                "quantity": str(quantity),
            }
        }
        session.save()

    def checkout(self, client=None, **fields):
        data = {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "bank", "respond": "json"}
        data.update(fields)
        return (client or self.client).post(reverse("pos_screen"), data, **AJAX)

    def test_bank_sale_without_a_receipt_is_refused_and_nothing_moves(self):
        self.set_cart()

        response = self.checkout()

        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "refused")
        self.assertEqual(data["receipt_error"], MISSING_MESSAGE)
        self.assertEqual(data["notice"]["text"], MISSING_MESSAGE)
        self.assertEqual(len(data["lines"]), 1)
        self.assertFalse(Sale.objects.exists())
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.stock, 20)
        self.assertIn(str(self.soap.id), self.client.session["cart"])

    def test_bank_sale_with_a_receipt_is_saved_with_its_proof(self):
        self.set_cart()

        response = self.checkout(bank_receipt=png("whatsapp screenshot.png"))

        data = response.json()
        self.assertTrue(data["ok"], data)
        sale = Sale.objects.get()
        self.assertEqual(data["redirect"], reverse("invoice", kwargs={"sale_id": sale.id}))
        receipt = get_receipt(sale)
        self.assertEqual(receipt.mime_type, "image/png")
        self.assertEqual(receipt.original_name, "whatsapp screenshot.png")
        self.assertEqual(receipt.uploaded_by, self.admin_user)
        self.assertEqual(receipt.size_bytes, len(image_bytes("PNG")))
        with receipt.file.open("rb") as stored:
            self.assertEqual(stored.read(), image_bytes("PNG"))
        # Recording the payment is its own entry; only later changes go to the audit log.
        self.assertFalse(AuditLog.objects.filter(action__startswith="bank_receipt").exists())

    def test_a_pdf_slip_is_accepted_at_the_till(self):
        self.set_cart()

        self.assertTrue(self.checkout(bank_receipt=pdf()).json()["ok"])

        self.assertTrue(get_receipt(Sale.objects.get()).is_pdf)

    def test_a_file_that_is_not_a_receipt_is_refused_with_the_reason(self):
        self.set_cart()

        data = self.checkout(bank_receipt=SimpleUploadedFile("notes.jpg", b"not a picture", content_type="image/jpeg")).json()

        self.assertFalse(data["ok"])
        self.assertIn("JPG, PNG or WEBP image, or a PDF", data["receipt_error"])
        self.assertFalse(Sale.objects.exists())

    def test_cash_sale_needs_no_receipt_and_ignores_a_stray_file(self):
        self.set_cart()

        data = self.checkout(payment_method="cash", bank_receipt=png()).json()

        self.assertTrue(data["ok"], data)
        self.assertFalse(BankReceipt.objects.exists())

    def test_credit_sale_needs_no_receipt_even_with_bank_selected(self):
        customer = Customer.objects.create(name="Ali Store")
        self.set_cart()

        data = self.checkout(is_credit="on", customer_id=customer.id).json()

        self.assertTrue(data["ok"], data)
        self.assertTrue(Sale.objects.get().is_credit)

    def test_with_the_rule_off_the_receipt_is_optional(self):
        set_rule(False)
        self.set_cart()

        self.assertTrue(self.checkout().json()["ok"])
        self.assertIsNone(get_receipt(Sale.objects.get()))

        self.set_cart()
        self.assertTrue(self.checkout(bank_receipt=jpg()).json()["ok"])
        self.assertEqual(get_receipt(Sale.objects.order_by("-id").first()).mime_type, "image/jpeg")

    def test_the_plain_form_post_is_held_to_the_same_rule(self):
        self.set_cart()

        refused = self.client.post(
            reverse("pos_screen"), {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "bank"}
        )

        self.assertEqual(refused.status_code, 200)
        self.assertContains(refused, "Attach the bank receipt")
        self.assertFalse(Sale.objects.exists())

        saved = self.client.post(
            reverse("pos_screen"),
            {"action": "checkout", "discount_rate": "0", "tax_rate": "0", "payment_method": "bank", "bank_receipt": png()},
        )
        self.assertEqual(saved.status_code, 302)
        self.assertIsNotNone(get_receipt(Sale.objects.get()))

    def test_a_cashier_attaches_the_receipt_to_his_own_sale(self):
        cashier = self.user_with_role("till", CASHIER_ROLE)
        client = self.client_for(cashier)
        session = client.session
        session["cart"] = {
            str(self.soap.id): {"name": "Soap", "price": "100.00", "original_price": "100.00", "discount_percent": "0.00", "quantity": "1"}
        }
        session.save()

        data = self.checkout(client=client, bank_receipt=png()).json()

        self.assertTrue(data["ok"], data)
        self.assertEqual(get_receipt(Sale.objects.get()).uploaded_by, cashier)

    def test_a_receipt_that_cannot_be_stored_undoes_the_sale(self):
        self.set_cart()

        with mock.patch("django.core.files.storage.FileSystemStorage.save", side_effect=OSError("disk full")):
            with self.assertLogs("accounts.services.bank_receipts", level="ERROR"), self.assertLogs("pos.views.pos_views", level="ERROR"):
                data = self.checkout(bank_receipt=png()).json()

        self.assertFalse(data["ok"])
        self.assertIn("could not be stored", data["notice"]["text"])
        self.assertFalse(Sale.objects.exists())
        self.assertFalse(BankReceipt.objects.exists())
        self.soap.refresh_from_db()
        self.assertEqual(self.soap.stock, 20)

    def test_the_till_page_carries_the_receipt_field_inside_the_bank_ledger_field(self):
        page = self.client.get(reverse("pos_screen")).content.decode()

        self.assertIn('enctype="multipart/form-data" id="checkout-form"', page)
        self.assertIn('id="till-receipt" data-required="1" data-max-bytes="5242880" data-max-label="5 MB"', page)
        self.assertIn('name="bank_receipt" id="till-bank-receipt"', page)
        self.assertIn('accept="image/jpeg,image/png,image/webp,application/pdf"', page)
        wrapper = page.index('id="bank-account-wrapper"')
        self.assertLess(wrapper, page.index('id="till-receipt"'))
        self.assertLess(page.index('id="till-receipt"'), page.index('id="till-tender"'))
        self.assertLess(page.index("zehnms/js/till-receipt.js"), page.index("zehnms/js/till.js"))

        set_rule(False)
        optional = self.client.get(reverse("pos_screen")).content.decode()
        self.assertIn('id="till-receipt" data-required="0"', optional)


# ---------------------------------------------------------------------------- purchases

class PurchaseReceiptTests(ReceiptCase):
    def setUp(self):
        super().setUp()
        self.supplier = Supplier.objects.create(name="ACME")

    def payload(self, *, method="bank", paid="200.00", item_id=None, **extra):
        line = {"product": self.soap.id, "quantity": 5, "cost_price": "40.00", "sale_price": "100.00"}
        data = {
            "supplier": self.supplier.id,
            "payment_method": method,
            "paid_amount": paid,
            "items-TOTAL_FORMS": "1",
            "items-INITIAL_FORMS": "1" if item_id else "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "1000",
        }
        if item_id:
            line["id"] = item_id
        for key, value in line.items():
            data[f"items-0-{key}"] = str(value)
        data.update(extra)
        return data

    def test_purchase_paid_by_bank_is_refused_without_a_receipt(self):
        response = self.client.post(reverse("purchase_add"), self.payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["purchase_form"].errors["bank_receipt"], [MISSING_MESSAGE])
        self.assertContains(response, MISSING_MESSAGE)
        self.assertFalse(Purchase.objects.exists())

    def test_part_payment_by_bank_needs_the_receipt_too(self):
        response = self.client.post(reverse("purchase_add"), self.payload(paid="50.00"))

        self.assertIn("bank_receipt", response.context["purchase_form"].errors)
        self.assertFalse(Purchase.objects.exists())

    def test_purchase_paid_by_bank_is_saved_with_its_receipt(self):
        response = self.client.post(reverse("purchase_add"), self.payload(bank_receipt=pdf()))

        self.assertEqual(response.status_code, 302)
        purchase = Purchase.objects.get()
        receipt = get_receipt(purchase)
        self.assertEqual(receipt.mime_type, "application/pdf")
        self.assertEqual(receipt.uploaded_by, self.admin_user)

        detail = self.client.get(reverse("purchase_detail", args=[purchase.id]))
        self.assertContains(detail, f'href="{self.receipt_page(purchase)}?next=')
        self.assertContains(detail, "Bank receipt")

    def test_a_bad_file_is_named_beside_the_field(self):
        bad = SimpleUploadedFile("slip.pdf", b"plain words", content_type="application/pdf")

        response = self.client.post(reverse("purchase_add"), self.payload(bank_receipt=bad))

        self.assertIn("JPG, PNG or WEBP image, or a PDF", response.context["purchase_form"].errors["bank_receipt"][0])
        self.assertFalse(Purchase.objects.exists())

    def test_cash_and_fully_credit_purchases_need_none(self):
        cash = self.client.post(reverse("purchase_add"), self.payload(method="cash"))
        on_credit = self.client.post(reverse("purchase_add"), self.payload(paid="0.00"))

        self.assertEqual(cash.status_code, 302)
        self.assertEqual(on_credit.status_code, 302)
        self.assertEqual(Purchase.objects.count(), 2)
        self.assertFalse(BankReceipt.objects.exists())

    def test_rule_off_makes_it_optional(self):
        set_rule(False)

        response = self.client.post(reverse("purchase_add"), self.payload())

        self.assertEqual(response.status_code, 302)

    def test_an_older_bank_purchase_can_be_corrected_without_a_receipt(self):
        purchase = self.bank_purchase(paid="200.00")
        item = purchase.items.get()
        payload = self.payload(item_id=item.id, reason="Fixing the sale price")
        payload["supplier"] = purchase.supplier_id

        response = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)

        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and response.context["purchase_form"].errors)

    def test_turning_a_purchase_into_a_bank_payment_needs_the_receipt(self):
        purchase = self.bank_purchase(paid="0.00")
        Purchase.objects.filter(pk=purchase.pk).update(payment_method="cash")
        item = purchase.items.get()
        payload = self.payload(item_id=item.id, reason="Paid by transfer today")
        payload["supplier"] = purchase.supplier_id

        refused = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)
        self.assertEqual(refused.context["purchase_form"].errors["bank_receipt"], [MISSING_MESSAGE])

        payload["bank_receipt"] = png()
        saved = self.client.post(reverse("purchase_edit", args=[purchase.id]), payload)
        self.assertEqual(saved.status_code, 302)
        self.assertIsNotNone(get_receipt(purchase))
        self.assertTrue(AuditLog.objects.filter(action="bank_receipt_attached", target_id=str(purchase.id)).exists())


# ---------------------------------------------------------------------------- settlements

class SettlementReceiptTests(ReceiptCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.customer = Customer.objects.create(name="Ali Store")
        self.supplier = Supplier.objects.create(name="ACME")
        self.bank = BankAccount.objects.create(name="Meezan", account_number="0101")

    def test_bank_settlement_sent_as_json_is_refused_without_a_receipt(self):
        response = self.client.post(
            reverse("settlement_api"),
            data=json.dumps({"customer_id": self.customer.id, "amount": "500.00", "payment_method": "bank", "bank_account_id": self.bank.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"bank_receipt": [MISSING_MESSAGE]})
        self.assertFalse(Settlement.objects.exists())

    def test_customer_receipt_through_a_bank_ledger_keeps_its_proof(self):
        response = self.client.post(
            reverse("settlement_api"),
            {"customer_id": self.customer.id, "amount": "500.00", "payment_method": "bank", "bank_account_id": self.bank.id, "bank_receipt": png()},
        )

        self.assertEqual(response.status_code, 201, response.content)
        settlement = Settlement.objects.get()
        self.assertEqual(settlement.bank_account, self.bank)
        self.assertEqual(get_receipt(settlement).mime_type, "image/png")

    def test_supplier_payment_through_a_bank_is_held_to_the_same_rule(self):
        refused = self.client.post(reverse("settlement_api"), {"supplier_id": self.supplier.id, "amount": "300.00", "payment_method": "bank"})
        self.assertEqual(refused.status_code, 400)

        saved = self.client.post(
            reverse("settlement_api"),
            {"supplier_id": self.supplier.id, "amount": "300.00", "payment_method": "bank", "bank_receipt": pdf()},
        )
        self.assertEqual(saved.status_code, 201, saved.content)
        self.assertTrue(get_receipt(Settlement.objects.get()).is_pdf)

    def test_a_bad_file_is_refused_and_no_money_is_posted(self):
        response = self.client.post(
            reverse("settlement_api"),
            {"customer_id": self.customer.id, "amount": "500.00", "payment_method": "bank", "bank_receipt": SimpleUploadedFile("x.png", b"nope")},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("bank_receipt", response.json())
        self.assertFalse(Settlement.objects.exists())

    def test_cash_settlement_and_rule_off_need_none(self):
        cash = self.client.post(
            reverse("settlement_api"),
            data=json.dumps({"customer_id": self.customer.id, "amount": "100.00", "payment_method": "cash"}),
            content_type="application/json",
        )
        self.assertEqual(cash.status_code, 201)

        set_rule(False)
        bank = self.client.post(
            reverse("settlement_api"),
            data=json.dumps({"customer_id": self.customer.id, "amount": "100.00", "payment_method": "bank"}),
            content_type="application/json",
        )
        self.assertEqual(bank.status_code, 201)
        self.assertFalse(BankReceipt.objects.exists())

    def test_statement_rows_say_which_bank_payments_have_their_receipt(self):
        with_proof = Settlement.objects.get(pk=create_receipt(party=self.customer, amount=Decimal("500"), payment_method="bank").ref_id)
        attach_receipt(with_proof, png(), actor=self.admin_user)
        without = Settlement.objects.get(pk=create_receipt(party=self.customer, amount=Decimal("200"), payment_method="bank").ref_id)
        create_receipt(party=self.customer, amount=Decimal("50"), payment_method="cash")

        rows = self.client.get(reverse("party_statement_api"), {"customer_id": self.customer.id}).json()["rows"]

        by_ref = {row["ref"]: row["receipt"] for row in rows}
        self.assertEqual(by_ref[f"Settlement#{with_proof.id}"], {"url": self.receipt_page(with_proof), "attached": True})
        self.assertEqual(by_ref[f"Settlement#{without.id}"], {"url": self.receipt_page(without), "attached": False})
        self.assertEqual(sum(1 for receipt in by_ref.values() if receipt is None), 1)

    def test_the_settle_form_and_the_printable_statement_show_the_receipt(self):
        settlement = Settlement.objects.get(pk=create_receipt(party=self.customer, amount=Decimal("500"), payment_method="bank").ref_id)
        attach_receipt(settlement, png(), actor=self.admin_user)

        ledger_page = self.client.get(reverse("customer_detail", args=[self.customer.id]))
        self.assertContains(ledger_page, 'id="settle_bank_receipt"')
        self.assertContains(ledger_page, "data-settle-receipt")

        statement = self.client.get(reverse("party_statement", args=["customer", self.customer.id]))
        self.assertContains(statement, f'href="{self.receipt_page(settlement)}?next=')

    def test_bank_ledger_rows_link_to_the_receipt(self):
        settlement = Settlement.objects.get(
            pk=create_receipt(party=self.customer, amount=Decimal("500"), payment_method="bank", bank_account=self.bank).ref_id
        )
        attach_receipt(settlement, png(), actor=self.admin_user)

        ledger = self.client.get(reverse("account_ledger", args=[self.bank.account_id]))

        self.assertContains(ledger, f'href="{self.receipt_page(settlement)}?next=')


# ---------------------------------------------------------------------------- expenses

class ExpenseReceiptTests(ReceiptCase):
    def setUp(self):
        super().setUp()
        self.rent = ExpenseCategory.objects.create(name="Shop Rent")

    def expense_payload(self, **extra):
        data = {
            "action": "add_expense",
            "expense-category": str(self.rent.id),
            "expense-title": "March rent",
            "expense-date": timezone.localdate().isoformat(),
            "expense-amount": "5000.00",
            "expense-paid_amount": "2000.00",
            "expense-payment_method": "bank",
        }
        data.update(extra)
        return data

    def test_expense_paid_by_bank_is_refused_without_a_receipt(self):
        response = self.client.post(reverse("expense_list"), self.expense_payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["expense_form"].errors["bank_receipt"], [MISSING_MESSAGE])
        self.assertContains(response, MISSING_MESSAGE)
        self.assertFalse(Expense.objects.exists())

    def test_expense_paid_by_bank_keeps_its_receipt_and_the_row_links_to_it(self):
        response = self.client.post(reverse("expense_list"), self.expense_payload(**{"expense-bank_receipt": jpg()}))

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertEqual(get_receipt(expense).mime_type, "image/jpeg")
        page = self.client.get(reverse("expense_list"))
        self.assertContains(page, f'href="{self.receipt_page(expense)}?next=')

    def test_an_unpaid_bill_needs_no_receipt(self):
        response = self.client.post(reverse("expense_list"), self.expense_payload(**{"expense-paid_amount": "0.00"}))

        self.assertEqual(response.status_code, 302)
        expense = Expense.objects.get()
        self.assertFalse(bank_receipts.is_bank_payment(expense))

    def test_paying_a_bill_by_bank_needs_the_receipt(self):
        expense = Expense.objects.create(category=self.rent, title="March rent", date=timezone.localdate(), amount=Decimal("5000"))
        post_expense(expense.id)
        payload = {
            "action": "pay_expense",
            "payment-expense": str(expense.id),
            "payment-date": timezone.localdate().isoformat(),
            "payment-amount": "5000.00",
            "payment-payment_method": "bank",
            "payment-bank_name": "Meezan Bank",
            "payment-bank_account_number": "44556677",
        }

        refused = self.client.post(reverse("expense_list"), payload)
        self.assertEqual(refused.context["expense_payment_form"].errors["bank_receipt"], [MISSING_MESSAGE])
        self.assertFalse(ExpensePayment.objects.exists())

        saved = self.client.post(reverse("money_desk"), {**payload, "payment-bank_receipt": pdf()})
        self.assertEqual(saved.status_code, 302)
        payment = ExpensePayment.objects.get()
        self.assertTrue(get_receipt(payment).is_pdf)
        # The bill itself was recorded unpaid: the bank movement and its proof belong to the payment.
        expense.refresh_from_db()
        self.assertFalse(bank_receipts.is_bank_payment(expense))
        self.assertContains(self.client.get(reverse("money_desk")), f'href="{self.receipt_page(payment)}?next=')

    def test_cash_expense_needs_none(self):
        response = self.client.post(reverse("expense_list"), self.expense_payload(**{"expense-payment_method": "cash"}))

        self.assertEqual(response.status_code, 302)
        self.assertFalse(BankReceipt.objects.exists())


# ------------------------------------------------- refunds and the money desk: never demanded

class OptionalReceiptTests(ReceiptCase):
    def test_bank_refund_on_a_sale_return_takes_a_receipt_but_does_not_demand_one(self):
        bank = BankAccount.objects.create(name="Meezan", account_number="0101")
        sale = self.bank_sale()
        item = sale.items.get()
        url = reverse("sale_return", args=[sale.id])
        fields = {"refund_method": "bank", "bank_account": bank.id, "reason": "Damaged", f"qty_{item.id}": "1"}

        self.assertContains(self.client.get(url), 'name="bank_receipt"')
        response = self.client.post(url, {**fields, "bank_receipt": png()})

        self.assertEqual(response.status_code, 302)
        sale_return = SaleReturn.objects.get()
        self.assertIsNotNone(get_receipt(sale_return))
        self.assertContains(self.client.get(url), f'href="{self.receipt_page(sale_return)}?next=')

        second_sale = self.bank_sale()
        second_item = second_sale.items.get()
        without = self.client.post(
            reverse("sale_return", args=[second_sale.id]),
            {"refund_method": "bank", "bank_account": bank.id, "reason": "Damaged", f"qty_{second_item.id}": "1"},
        )
        self.assertEqual(without.status_code, 302)
        self.assertEqual(SaleReturn.objects.count(), 2)

    def test_bank_refund_on_a_purchase_return_takes_a_receipt(self):
        bank = BankAccount.objects.create(name="Meezan", account_number="0101")
        purchase = self.bank_purchase(paid="200.00")
        item = purchase.items.get()

        response = self.client.post(
            reverse("purchase_return", args=[purchase.id]),
            {"settlement_method": "bank", "bank_account": bank.id, "reason": "Short weight", f"qty_{item.id}": "1", "bank_receipt": pdf()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(get_receipt(PurchaseReturn.objects.get()).is_pdf)

    def test_money_desk_transfer_and_funding_take_a_receipt_but_do_not_demand_one(self):
        today = timezone.localdate().isoformat()
        transfer = {
            "action": "transfer",
            "transfer-date": today,
            "transfer-amount": "150.00",
            "transfer-from_account": "cash",
            "transfer-to_account": "bank",
            "transfer-to_bank_name": "HBL",
            "transfer-to_bank_account_number": "99887766",
        }
        self.assertEqual(self.client.post(reverse("money_desk"), transfer).status_code, 302)
        self.assertEqual(self.client.post(reverse("money_desk"), {**transfer, "transfer-bank_receipt": png()}).status_code, 302)
        with_slip = MoneyTransfer.objects.order_by("-id").first()
        self.assertIsNotNone(get_receipt(with_slip))
        self.assertEqual(BankReceipt.objects.count(), 1)

        funding = {
            "action": "capital",
            "capital-source_name": "Owner Capital",
            "capital-date": today,
            "capital-amount": "10000.00",
            "capital-deposit_to": "bank",
            "capital-bank_name": "Meezan Bank",
            "capital-bank_account_number": "1234567890",
            "capital-bank_receipt": jpg(),
        }
        self.assertEqual(self.client.post(reverse("money_desk"), funding).status_code, 302)
        self.assertIsNotNone(get_receipt(CapitalEntry.objects.get()))

        page = self.client.get(reverse("money_desk"))
        self.assertContains(page, f'href="{self.receipt_page(with_slip)}?next=')


# ---------------------------------------------------------- seeing, adding, replacing, removing

class ReceiptPageTests(ReceiptCase):
    def test_the_invoice_shows_the_receipt_or_offers_to_add_it(self):
        sale = self.bank_sale()
        invoice = reverse("invoice", kwargs={"sale_id": sale.id})

        missing = self.client.get(invoice)
        self.assertContains(missing, "Bank receipt")
        self.assertContains(missing, f'href="{self.receipt_page(sale)}?next={invoice}"')
        self.assertContains(missing, "Attach")

        attach_receipt(sale, png(), actor=self.admin_user)
        self.assertContains(self.client.get(invoice), ">View</a>")

    def test_a_cash_sale_shows_no_receipt_line(self):
        sale = Sale.objects.create(payment_method="cash", sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.soap, quantity=1, price=self.soap.price, cost_price=self.soap.cost_price)

        self.assertNotContains(self.client.get(reverse("invoice", kwargs={"sale_id": sale.id})), "Bank receipt")

    def test_image_and_pdf_open_in_the_browser_from_a_checked_address(self):
        sale = self.bank_sale()
        attach_receipt(sale, png(), actor=self.admin_user)

        page = self.client.get(self.receipt_page(sale))
        self.assertContains(page, f'<img class="rc-image" src="{self.receipt_file(sale)}?v=')
        self.assertNotContains(page, "/media/")
        self.assertNotContains(page, get_receipt(sale).file.name)

        image = self.client.get(self.receipt_file(sale))
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image["Content-Type"], "image/png")
        self.assertTrue(image["Content-Disposition"].startswith("inline"))
        self.assertEqual(image["X-Content-Type-Options"], "nosniff")
        self.assertEqual(image["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("no-store", image["Cache-Control"])
        self.assertEqual(self.body(image), image_bytes("PNG"))

        purchase = self.bank_purchase()
        attach_receipt(purchase, pdf(), actor=self.admin_user)
        self.assertContains(self.client.get(self.receipt_page(purchase)), f'<iframe class="rc-pdf" src="{self.receipt_file(purchase)}?v=')
        slip = self.client.get(self.receipt_file(purchase))
        self.assertEqual(slip["Content-Type"], "application/pdf")
        self.assertTrue(slip["Content-Disposition"].startswith("inline"))
        self.assertEqual(self.body(slip), PDF_BYTES)

        download = self.client.get(self.receipt_file(purchase), {"download": "1"})
        self.assertTrue(download["Content-Disposition"].startswith("attachment"))
        self.assertIn(f"bank-receipt-purchase-{purchase.id}.pdf", download["Content-Disposition"])
        self.body(download)

    def test_a_visitor_who_is_not_signed_in_is_sent_to_the_sign_in_page(self):
        sale = self.bank_sale()
        attach_receipt(sale, png(), actor=self.admin_user)
        anonymous = Client()

        for url in (self.receipt_page(sale), self.receipt_file(sale)):
            response = anonymous.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response.url)

    def test_a_cashier_sees_the_proof_of_his_own_sales_only(self):
        cashier = self.user_with_role("till", CASHIER_ROLE)
        own = self.bank_sale(sold_by=cashier)
        other = self.bank_sale(sold_by=self.admin_user)
        attach_receipt(own, png(), actor=cashier)
        attach_receipt(other, png(), actor=self.admin_user)
        client = self.client_for(cashier)

        self.assertEqual(client.get(self.receipt_page(own)).status_code, 200)
        self.body(client.get(self.receipt_file(own)))
        self.assertEqual(client.get(self.receipt_file(other)).status_code, 403)
        self.assertEqual(client.get(self.receipt_page(other)).status_code, 302)

    def test_a_role_only_opens_receipts_of_the_areas_it_can_open(self):
        buyer = self.custom_role_user("buyer", purchases="view")
        client = self.client_for(buyer)
        sale = self.bank_sale()
        purchase = self.bank_purchase()
        attach_receipt(sale, png(), actor=self.admin_user)
        attach_receipt(purchase, png(), actor=self.admin_user)
        expense = Expense.objects.create(
            category=ExpenseCategory.objects.create(name="Rent"), date=timezone.localdate(),
            amount=Decimal("900"), paid_amount=Decimal("900"), payment_method="bank",
        )
        attach_receipt(expense, pdf(), actor=self.admin_user)

        self.assertEqual(client.get(self.receipt_file(purchase)).status_code, 200)
        self.assertEqual(client.get(self.receipt_file(sale)).status_code, 403)
        self.assertEqual(client.get(self.receipt_file(expense)).status_code, 403)

    def test_the_proof_can_be_added_later_from_the_place_it_is_viewed(self):
        sale = self.bank_sale()
        invoice = reverse("invoice", kwargs={"sale_id": sale.id})
        page = f"{self.receipt_page(sale)}?next={invoice}"

        offered = self.client.get(page)
        self.assertContains(offered, "No bank receipt attached")
        self.assertContains(offered, "Attach receipt")
        self.assertContains(offered, f'href="{invoice}"')

        response = self.client.post(page, {"next": invoice, "bank_receipt": png("late proof.png")})

        self.assertRedirects(response, invoice, fetch_redirect_response=False)
        self.assertEqual(get_receipt(sale).original_name, "late proof.png")
        audit = AuditLog.objects.get(action="bank_receipt_attached")
        self.assertEqual(audit.actor, self.admin_user)
        self.assertEqual((audit.target_model, audit.target_id), ("Sale", str(sale.id)))
        self.assertEqual(audit.metadata["payment_kind"], "sale")
        self.assertEqual(audit.metadata["file_name"], "late proof.png")
        self.assertEqual(len(audit.metadata["sha256"]), 64)

    def test_adding_later_is_refused_for_a_bad_file_and_says_why_beside_the_field(self):
        sale = self.bank_sale()

        response = self.client.post(self.receipt_page(sale), {"bank_receipt": SimpleUploadedFile("a.png", b"words")})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "JPG, PNG or WEBP image, or a PDF")
        self.assertIsNone(get_receipt(sale))
        self.assertContains(self.client.post(self.receipt_page(sale), {}), "Choose the receipt file first")

    def test_a_viewer_may_add_a_missing_proof_but_not_replace_or_remove_one(self):
        buyer = self.custom_role_user("buyer", purchases="view")
        client = self.client_for(buyer)
        purchase = self.bank_purchase()

        added = client.post(self.receipt_page(purchase), {"bank_receipt": png("first.png")})
        self.assertEqual(added.status_code, 302)
        first = get_receipt(purchase)
        self.assertEqual(first.uploaded_by, buyer)

        page = client.get(self.receipt_page(purchase))
        self.assertNotContains(page, "Replace receipt")
        self.assertNotContains(page, "Remove receipt")

        client.post(self.receipt_page(purchase), {"bank_receipt": png("second.png")})
        client.post(self.receipt_page(purchase), {"action": "remove"})
        self.assertEqual(get_receipt(purchase), first)
        self.assertEqual(BankReceipt.objects.count(), 1)
        self.assertFalse(AuditLog.objects.filter(action__in=("bank_receipt_replaced", "bank_receipt_removed")).exists())

    def test_replacing_needs_manage_access_keeps_the_old_file_and_is_audited(self):
        manager = self.custom_role_user("manager", purchases="manage")
        client = self.client_for(manager)
        purchase = self.bank_purchase()
        first = attach_receipt(purchase, png("first.png"), actor=self.admin_user, audit=False)

        response = client.post(self.receipt_page(purchase), {"bank_receipt": pdf("second.pdf")})

        self.assertEqual(response.status_code, 302)
        current = get_receipt(purchase)
        self.assertEqual(current.original_name, "second.pdf")
        first.refresh_from_db()
        self.assertIsNotNone(first.removed_at)
        self.assertEqual(first.removed_by, manager)
        self.assertTrue(Path(first.file.path).is_file())
        audit = AuditLog.objects.get(action="bank_receipt_replaced")
        self.assertEqual(audit.actor, manager)
        self.assertEqual(audit.metadata["replaced_receipt_ids"], [first.id])
        self.assertEqual(audit.metadata["replaced_sha256"], [first.sha256])
        self.assertEqual(audit.metadata["receipt_id"], current.id)

    def test_removing_asks_first_needs_manage_access_and_is_audited(self):
        sale = self.bank_sale()
        receipt = attach_receipt(sale, png(), actor=self.admin_user, audit=False)
        page = self.receipt_page(sale)

        asking = self.client.get(page, {"confirm": "remove"})
        self.assertContains(asking, 'name="action" value="remove"')
        self.assertIsNotNone(get_receipt(sale))

        response = self.client.post(page, {"action": "remove"})

        self.assertRedirects(response, page, fetch_redirect_response=False)
        self.assertIsNone(get_receipt(sale))
        receipt.refresh_from_db()
        self.assertEqual(receipt.removed_by, self.admin_user)
        self.assertTrue(Path(receipt.file.path).is_file())
        audit = AuditLog.objects.get(action="bank_receipt_removed")
        self.assertEqual((audit.target_model, audit.target_id), ("Sale", str(sale.id)))
        self.assertEqual(audit.metadata["receipt_id"], receipt.id)
        self.assertEqual(self.client.get(self.receipt_file(sale)).status_code, 404)

    def test_a_cashier_manages_the_proof_of_his_own_sale_but_not_of_another(self):
        cashier = self.user_with_role("till", CASHIER_ROLE)
        client = self.client_for(cashier)
        other = self.bank_sale(sold_by=self.admin_user)
        receipt = attach_receipt(other, png(), actor=self.admin_user, audit=False)

        client.post(self.receipt_page(other), {"action": "remove"})
        client.post(self.receipt_page(other), {"bank_receipt": png()})

        self.assertEqual(get_receipt(other), receipt)

        own = self.bank_sale(sold_by=cashier)
        attach_receipt(own, png("old.png"), actor=cashier, audit=False)
        client.post(self.receipt_page(own), {"bank_receipt": png("new.png")})
        self.assertEqual(get_receipt(own).original_name, "new.png")

    def test_a_payment_that_is_not_a_bank_payment_takes_no_receipt(self):
        sale = Sale.objects.create(payment_method="cash", sold_by=self.admin_user)
        SaleItem.objects.create(sale=sale, product=self.soap, quantity=1, price=self.soap.price, cost_price=self.soap.cost_price)

        page = self.client.get(self.receipt_page(sale))
        self.assertContains(page, "did not go through a bank")
        self.assertNotContains(page, 'type="file"')

        self.client.post(self.receipt_page(sale), {"bank_receipt": png()})
        self.assertFalse(BankReceipt.objects.exists())

    def test_unknown_kinds_and_missing_payments_are_not_found(self):
        self.assertEqual(self.client.get(reverse("bank_receipt", args=["voucher", 1])).status_code, 404)
        self.assertEqual(self.client.get(reverse("bank_receipt", args=["sale", 999999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("bank_receipt_file", args=["sale", 999999])).status_code, 404)

    def test_next_only_leads_back_to_this_site(self):
        sale = self.bank_sale()

        response = self.client.post(
            f"{self.receipt_page(sale)}?next=https://evil.example/", {"next": "https://evil.example/", "bank_receipt": png()}
        )

        self.assertRedirects(response, reverse("invoice", kwargs={"sale_id": sale.id}), fetch_redirect_response=False)

    def test_a_receipt_whose_file_is_gone_says_so_instead_of_breaking(self):
        sale = self.bank_sale()
        receipt = attach_receipt(sale, png(), actor=self.admin_user)
        Path(receipt.file.path).unlink()

        page = self.client.get(self.receipt_page(sale))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "not in this shop's storage")
        self.assertNotContains(page, "rc-image")
        self.assertEqual(self.client.get(self.receipt_file(sale)).status_code, 404)

    def test_marks_cost_a_fixed_number_of_queries_for_a_page_of_rows(self):
        sales = [self.bank_sale() for _ in range(6)]
        attach_receipt(sales[0], png(), actor=self.admin_user)
        refs = [("Sale", sale.id) for sale in sales]
        receipt_marks(refs)  # content types are cached after the first use

        with self.assertNumQueries(2):
            marks = receipt_marks(refs)

        self.assertEqual(len(marks), 6)
        self.assertTrue(marks[("Sale", sales[0].id)]["attached"])
        self.assertFalse(marks[("Sale", sales[1].id)]["attached"])

    def test_removing_through_the_service_reports_whether_there_was_one(self):
        sale = self.bank_sale()

        self.assertFalse(remove_receipt(sale, actor=self.admin_user))
        attach_receipt(sale, png(), actor=self.admin_user)
        self.assertTrue(remove_receipt(sale, actor=self.admin_user))


class ReceiptFilesHaveNoPublicAddressTests(ReceiptCase):
    def setUp(self):
        super().setUp()
        root = Path(self._media_root)
        (root / "bank_receipts" / "2026" / "11").mkdir(parents=True, exist_ok=True)
        (root / "bank_receipts" / "2026" / "11" / "abc.jpg").write_bytes(image_bytes("JPEG"))
        (root / "company_logos").mkdir(exist_ok=True)
        (root / "company_logos" / "logo.png").write_bytes(image_bytes("PNG"))
        self.request = RequestFactory().get("/media/anything")

    def serve(self, path):
        return serve_public_media(self.request, path, document_root=self._media_root)

    def test_the_logo_is_still_served(self):
        response = self.serve("company_logos/logo.png")

        self.assertEqual(response.status_code, 200)
        self.body(response)

    def test_receipts_are_not_served_whatever_way_the_path_is_written(self):
        for path in (
            "bank_receipts/2026/11/abc.jpg",
            "company_logos/../bank_receipts/2026/11/abc.jpg",
            "./bank_receipts//2026/11/abc.jpg",
            "company_logos\\..\\bank_receipts\\2026\\11\\abc.jpg",
            "BANK_RECEIPTS/2026/11/abc.jpg",
            "bank_receipts",
        ):
            with self.subTest(path=path), self.assertRaises(Http404):
                self.serve(path)

    def test_the_media_route_goes_through_that_guard(self):
        if not settings.SERVE_MEDIA_FILES:
            self.skipTest("Django does not serve /media/ in this configuration.")

        match = resolve("/media/bank_receipts/2026/11/abc.jpg")

        self.assertIs(match.func, serve_public_media)
        self.assertEqual(Client().get("/media/bank_receipts/2026/11/abc.jpg").status_code, 404)


# ---------------------------------------------------------------------------- the setting

class RequireBankReceiptSettingTests(ReceiptCase):
    def test_the_rule_is_on_by_default_and_shown_in_settings(self):
        self.assertTrue(SystemSettings.load().require_bank_receipt)

        page = self.client.get(reverse("settings_page"))

        self.assertContains(page, "Require a receipt for bank payments")
        self.assertContains(page, 'name="business-require_bank_receipt"')

    def test_the_owner_can_switch_it_off_and_the_change_is_audited(self):
        response = self.client.post(
            reverse("settings_page"),
            {
                "action": "save_rules",
                "business-tax_mode": SystemSettings.MANUAL_CART,
                "business-tax_label": "GST",
                "business-allow_credit_sales": "on",
                "business-default_sale_payment_method": "cash",
                "business-thermal_printer_width_mm": "58",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(SystemSettings.load().require_bank_receipt)
        audit = AuditLog.objects.filter(action="system_settings_updated").latest("id")
        self.assertIs(audit.metadata["require_bank_receipt"], False)

        self.client.post(
            reverse("settings_page"),
            {
                "action": "save_rules",
                "business-tax_mode": SystemSettings.MANUAL_CART,
                "business-tax_label": "GST",
                "business-default_sale_payment_method": "cash",
                "business-thermal_printer_width_mm": "58",
                "business-require_bank_receipt": "on",
            },
        )
        self.assertTrue(SystemSettings.load().require_bank_receipt)
