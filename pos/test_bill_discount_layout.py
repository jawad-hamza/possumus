"""Bills show each line's discount in % and rupees, and the bill discount straight after the subtotal."""
import re
from decimal import Decimal

from django.urls import reverse

from pos.models import Product, Sale, SaleItem
from pos.test_support import AuthenticatedAdminTestCase


class BillDiscountLayoutTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        product = Product.objects.create(name="Soap", sku="SOAP", cost_price=Decimal("50"), price=Decimal("100"), stock=10)
        self.sale = Sale.objects.create(payment_method=Sale.CASH, discount_rate=Decimal("5"))
        SaleItem.objects.create(sale=self.sale, product=product, quantity=Decimal("2"), price=Decimal("100"), discount_percent=Decimal("10"))
        self.sale.refresh_from_db()

    def test_invoice_lines_carry_the_discount_and_the_bill_discount_follows_the_subtotal(self):
        page = self.client.get(reverse("invoice", args=[self.sale.id])).content.decode()
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))

        # Thermal receipt line: gross, then the discount in % and rupees with the net amount.
        self.assertIn("2 x Rs 100.00 Rs 200.00", text)
        self.assertIn("Disc 10% = Rs 20.00 Rs 180.00", text)
        # A4 line: rate before discount, a Disc column with both figures, the net amount.
        self.assertIn('<th scope="col" class="a-num">Disc</th>', page)
        self.assertRegex(text, r"Rs 100\.00 10% Rs 20\.00 Rs 180\.00")
        # The old summary row is gone and the bill discount sits right after the subtotal.
        self.assertNotIn("Line discounts, included", text)
        self.assertRegex(text, r"Subtotal Rs 180\.00 Discount 5% = Rs 9\.00")

    def test_thermal_pdf_still_renders(self):
        response = self.client.get(reverse("invoice_pdf", args=[self.sale.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
