from django.apps import AppConfig


class DistributionConfig(AppConfig):
    """A distributor's day: orders booked on routes, loads, day-end settlement, stock returns, recovery."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "distribution"
    verbose_name = "Distribution"
