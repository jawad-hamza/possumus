"""Which scheme a line gets: trigger, repeat, value, specificity, dates, scope and the minimum margin."""
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError

from pos.models import Customer
from pricing.models import Scheme
from pricing.services.schemes import SchemeBook

from .base import PACK_BOX, PACK_CARTON, PACK_PIECE, PricingCase


class SchemeSelectionTests(PricingCase):
    def offer(self, quantity, level=PACK_CARTON, customer=None, rate=None, **flags):
        book = SchemeBook(customer)
        return book.best_offer(self.soap, level, Decimal(str(quantity)), rate or self.soap.pack_price(level), **flags)

    def test_free_goods_need_the_trigger_and_repeat(self):
        self.scheme("Soap 10+1")
        self.assertIsNone(self.offer(9))
        offer = self.offer(10)
        self.assertEqual((offer.kind, offer.free_quantity, offer.free_level), (Scheme.REWARD_FREE, Decimal("1"), PACK_CARTON))
        self.assertEqual(self.offer(29).free_quantity, Decimal("2"))
        # The trigger is counted in pieces: 100 boxes are 10 cartons.
        self.assertEqual(self.offer(100, PACK_BOX).free_quantity, Decimal("1"))
        # The free goods are valued at the customer's rate: one carton of 1,440.
        self.assertEqual(self.offer(10).value, Decimal("1440.00"))

    def test_the_offer_worth_most_wins(self):
        self.scheme("Soap 10+1")  # worth 1,440 on 10 cartons
        self.scheme("Soap 5% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("5"))
        # On 10 cartons 5% is 720: the free carton wins.
        self.assertEqual(self.offer(10).scheme.name, "Soap 10+1")
        # On 8 cartons only the discount applies.
        self.assertEqual(self.offer(8).scheme.name, "Soap 5% from 5")
        # A slab that is worth more wins on its own: 12% on 10 cartons is 1,728.
        self.scheme("Soap 12% from 10", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("10"), discount_percent=Decimal("12"))
        self.assertEqual(self.offer(10).scheme.name, "Soap 12% from 10")

    def test_a_tie_goes_to_the_more_specific_scheme(self):
        self.scheme("Everyone 10+1")
        self.scheme("Ali 10+1", scope=Scheme.SCOPE_CUSTOMER, customer=self.shop)
        self.scheme("Category 10+1", product=None, category=self.cleaning)
        self.assertEqual(self.offer(10, customer=self.shop).scheme.name, "Ali 10+1")
        self.assertEqual(self.offer(10, customer=self.other_shop).scheme.name, "Everyone 10+1")
        self.assertEqual(self.offer(10).scheme.name, "Everyone 10+1")

    def test_scope_dates_and_switch(self):
        today = self.today
        route_scheme = self.scheme("Route 10+1", scope=Scheme.SCOPE_ROUTE, route=self.route)
        self.assertEqual(self.offer(10, customer=Customer.objects.get(pk=self.shop.pk)).scheme, route_scheme)
        self.assertIsNone(self.offer(10, customer=self.other_shop))
        self.assertIsNone(self.offer(10))  # a walk-in only gets schemes for everyone
        route_scheme.start_date = today + timedelta(days=1)
        route_scheme.save()
        self.assertIsNone(self.offer(10, customer=self.shop))
        route_scheme.start_date, route_scheme.end_date = today - timedelta(days=10), today - timedelta(days=1)
        route_scheme.save()
        self.assertIsNone(self.offer(10, customer=self.shop))
        route_scheme.end_date = today
        route_scheme.is_active = False
        route_scheme.save()
        self.assertIsNone(self.offer(10, customer=self.shop))

    def test_a_category_scheme_reads_the_trigger_in_each_products_packing(self):
        self.biscuit.category = self.cleaning
        self.biscuit.save()
        self.scheme("Cleaning 2% from 2 ctn", product=None, category=self.cleaning, reward=Scheme.REWARD_PERCENT,
                    trigger_quantity=Decimal("2"), discount_percent=Decimal("2"))
        book = SchemeBook(None)
        # Biscuits have no cartons: the trigger is read as 2 pieces.
        self.assertIsNotNone(book.best_offer(self.biscuit, PACK_PIECE, Decimal("2"), Decimal("30")))
        self.assertIsNotNone(book.best_offer(self.soap, PACK_CARTON, Decimal("2"), Decimal("1440")))
        self.assertIsNone(book.best_offer(self.soap, PACK_BOX, Decimal("19"), Decimal("144")))

    def test_discounts_never_break_the_minimum_margin_and_never_override_what_was_typed(self):
        self.soap.minimum_margin_percent = Decimal("18")  # lowest carton price 1,416
        self.soap.save()
        self.scheme("Soap 5% from 5", reward=Scheme.REWARD_PERCENT, trigger_quantity=Decimal("5"), discount_percent=Decimal("5"))
        self.scheme("Soap 10 off a ctn", reward=Scheme.REWARD_AMOUNT, trigger_quantity=Decimal("5"), discount_amount=Decimal("10"))
        offer = self.offer(5)
        # 5% would make 1,368, below 1,416: passed over for the amount off.
        self.assertEqual((offer.kind, offer.amount_off), (Scheme.REWARD_AMOUNT, Decimal("10.00")))
        # The amount is per carton: a box line gets a tenth of it.
        self.assertEqual(self.offer(50, PACK_BOX).amount_off, Decimal("1.00"))
        # A rate typed by hand keeps the amount-off scheme away.
        self.assertIsNone(self.offer(5, allow_amount=False))

    def test_the_module_switch_turns_schemes_off(self):
        self.scheme("Soap 10+1")
        self.module("schemes", False)
        self.assertIsNone(self.offer(10))

    def test_a_scheme_must_say_what_it_gives(self):
        scheme = Scheme(name="Broken", product=self.soap, reward=Scheme.REWARD_FREE, free_quantity=None, trigger_quantity=Decimal("10"))
        with self.assertRaises(ValidationError):
            scheme.full_clean()
        both = Scheme(name="Both", product=self.soap, category=self.cleaning, reward=Scheme.REWARD_PERCENT, discount_percent=Decimal("2"))
        with self.assertRaises(ValidationError):
            both.full_clean()
        self.assertEqual(self.scheme("Soap 10+1").offer_label, "10 Ctn + 1 Ctn free")
