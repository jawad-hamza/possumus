from django.apps import AppConfig


class PricingConfig(AppConfig):
    """Trade price lists for customers and routes, and schemes: free goods and quantity discounts."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "pricing"
    verbose_name = "Price lists and schemes"
