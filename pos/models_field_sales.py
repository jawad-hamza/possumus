"""Field-sales master data for a distribution business: routes and order bookers.

Kept beside pos/models.py (which imports these two classes) so the models of the
switchable "field_sales" module live in one place. They belong to the ``pos`` app like
every other model there.
"""
from django.db import models
from django.utils import timezone


class Route(models.Model):
    """The shops an order booker visits together. A customer sits on at most one route."""

    name = models.CharField(max_length=100)
    city = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)
    # The price list customers on this route buy at, unless they have one of their own.
    price_list = models.ForeignKey(
        "pricing.PriceList", null=True, blank=True, on_delete=models.SET_NULL, related_name="routes"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name

    @property
    def label(self):
        """Name with its city, for lists where two cities may each have a "Main Bazaar"."""
        return f"{self.name} ({self.city})" if self.city else self.name


class OrderBooker(models.Model):
    """A person who takes orders from shops. Not a login: a name on customers, sales and reports."""

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    routes = models.ManyToManyField(Route, blank=True, related_name="order_bookers")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name
