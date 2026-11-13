"""The distributor of the distribution tests (soap 10 boxes x 12 pieces a carton, biscuits by the
piece, a shop on a route) with price lists and schemes on top."""
from decimal import Decimal

from django.urls import reverse

from distribution.tests.base import DistributionCase
from pos.models import PACK_BOX, PACK_CARTON, PACK_PIECE, Category, Product, Sale
from pos.services.modules import set_module_switch

from pricing.models import PriceList, PriceListItem, Scheme

ZERO = Decimal("0")


class PricingCase(DistributionCase):
    def setUp(self):
        super().setUp()
        # 20 cartons on the shelf: cost 1,200 and price 1,440 a carton.
        self.soap.stock = Decimal("2400")
        self.soap.save()
        self.cleaning = Category.objects.create(name="Cleaning")
        self.soap.category = self.cleaning
        self.soap.save()

    # ------------------------------------------------------------------ helpers
    def price_list(self, name, *prices, active=True):
        """prices: (product, price per largest pack)."""
        price_list = PriceList.objects.create(name=name, is_active=active)
        for product, pack_price in prices:
            factor = product.pack_factor(product.top_pack_level)
            PriceListItem.objects.create(
                price_list=price_list, product=product, price=product.quantize_unit_money(Decimal(str(pack_price)) / factor)
            )
        return price_list

    def scheme(self, name="Scheme", **values):
        defaults = {
            "product": self.soap,
            "trigger_quantity": Decimal("10"),
            "trigger_unit": PACK_CARTON,
            "reward": Scheme.REWARD_FREE,
            "free_quantity": Decimal("1"),
            "free_unit": PACK_CARTON,
        }
        defaults.update(values)
        scheme = Scheme(name=name, **defaults)
        scheme.full_clean()
        scheme.save()
        return scheme

    def module(self, key, on):
        set_module_switch(key, on)

    def till(self, action, **data):
        response = self.client.post(reverse("till_action", args=[action]), data)
        return response.json()

    def add_to_bill(self, product, quantity, unit="ctn", customer=None):
        data = {"product_id": product.id, "quantity": str(quantity), "quantity_unit": unit}
        if customer is not None:
            data["customer_id"] = customer.id
        return self.till("add", **data)

    def checkout(self, customer=None, payment="cash", credit=False):
        data = {"action": "checkout", "payment_method": payment, "discount_rate": "0", "tax_rate": "0"}
        if customer is not None:
            data["customer_id"] = customer.id
        if credit:
            data["is_credit"] = "on"
        return self.client.post(reverse("pos_screen"), data)

    def cart(self):
        return self.client.session.get("cart", {})

    def last_sale(self):
        return Sale.objects.order_by("-id").first()


__all__ = ["PricingCase", "PACK_BOX", "PACK_CARTON", "PACK_PIECE", "ZERO", "Product", "Scheme"]
