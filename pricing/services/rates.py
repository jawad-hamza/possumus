"""Which price a customer buys at.

Resolution order for one product, for one customer:

1. the customer's own price list, when it is active and lists the product;
2. otherwise the price list of the customer's route, when it is active and lists the product;
3. otherwise the product's own sale price.

A list that is switched off, or a product missing from the list, falls through to the next
step. A walk-in sale (no customer) always uses the product price. With the "price_lists"
module off every customer buys at the product price.
"""
from decimal import Decimal

from pos.models import quantize_money
from pos.services.modules import module_enabled

from ..models import PriceList, PriceListItem


def price_lists_enabled():
    return module_enabled("price_lists")


def schemes_enabled():
    return module_enabled("schemes")


def customer_price_lists(customer):
    """The lists that apply to a customer, most specific first: [customer's, route's]."""
    if customer is None or not price_lists_enabled():
        return []
    found = []
    if customer.price_list_id:
        price_list = customer.price_list
        if price_list.is_active:
            found.append(price_list)
    route = customer.route if customer.route_id else None
    if route is not None and route.price_list_id:
        price_list = route.price_list
        if price_list.is_active and price_list not in found:
            found.append(price_list)
    return found


def customer_price_list(customer):
    """The first list that applies to the customer, or None."""
    lists = customer_price_lists(customer)
    return lists[0] if lists else None


class RateBook:
    """Rates for one customer. Prices are read once per product and kept."""

    def __init__(self, customer=None):
        self.customer = customer
        self.lists = customer_price_lists(customer)
        self._prices = {}  # product id -> (base price, list) or None

    @property
    def key(self):
        """Names the lists in force, so a bill line knows when its rate must be looked up again."""
        return ",".join(str(price_list.pk) for price_list in self.lists) or "0"

    def load(self, product_ids):
        missing = [pk for pk in {int(pk) for pk in product_ids} if pk not in self._prices]
        if not missing:
            return
        for pk in missing:
            self._prices[pk] = None
        if not self.lists:
            return
        order = {price_list.pk: index for index, price_list in enumerate(self.lists)}
        rows = PriceListItem.objects.filter(price_list__in=self.lists, product_id__in=missing).values_list(
            "product_id", "price_list_id", "price"
        )
        best = {}
        for product_id, list_id, price in rows:
            rank = order[list_id]
            if product_id not in best or rank < best[product_id][0]:
                best[product_id] = (rank, Decimal(price), list_id)
        lists = {price_list.pk: price_list for price_list in self.lists}
        for product_id, (_, price, list_id) in best.items():
            self._prices[product_id] = (price, lists[list_id])

    def list_entry(self, product):
        self.load([product.pk])
        return self._prices.get(product.pk)

    def base_price(self, product):
        """Price of one base unit for this customer (six places)."""
        entry = self.list_entry(product)
        return entry[0] if entry else Decimal(product.price or 0)

    def price_list_for(self, product):
        entry = self.list_entry(product)
        return entry[1] if entry else None

    def rate(self, product, level):
        """Price of one unit of the level, as line money."""
        entry = self.list_entry(product)
        if entry is None:
            return product.pack_price(level)
        return quantize_money(entry[0] * product.pack_factor(level))


def customer_rate(product, level, customer=None):
    return RateBook(customer).rate(product, level)


def pricing_terms(customer):
    """What the order form needs when a customer is chosen: the list's name and its rates.

    ``rates`` is {product id: {level: rate}} for the products the customer's lists name; every
    other product keeps its own price.
    """
    book = RateBook(customer)
    if not book.lists:
        return {"price_list": "", "rates": {}}
    items = (
        PriceListItem.objects.filter(price_list__in=book.lists, product__is_active=True)
        .select_related("product")
        .order_by("id")
    )
    rates = {}
    product_ids = {item.product_id for item in items}
    book.load(product_ids)
    products = {item.product_id: item.product for item in items}
    for product_id, product in products.items():
        rates[str(product_id)] = {level["code"]: str(book.rate(product, level["code"])) for level in product.pack_levels}
    return {"price_list": book.lists[0].name, "rates": rates}


def active_price_lists():
    return PriceList.objects.filter(is_active=True).order_by("name")
