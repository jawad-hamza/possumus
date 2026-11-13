from django.apps import AppConfig


class ReviewConfig(AppConfig):
    """Review notes: testers pin a comment on any part of any screen; the owner reads them on one page."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "review"
    verbose_name = "Review notes"
