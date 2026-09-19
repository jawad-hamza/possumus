"""URLs of the table reports. Included from pos.urls under reports/."""
from django.urls import path

from .services.reporting import REPORTS
from .views.analytics_reports import analytics_report

urlpatterns = [
    path(f"{spec.slug}/", analytics_report, {"slug": spec.slug}, name=spec.url_name)
    for spec in REPORTS
]
