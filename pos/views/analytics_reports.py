"""The table reports registered in pos.services.reporting: one view serves them all.

Each one shares the same page shell (report switcher, date shortcuts, CSV export), so
moving between reports keeps the dates the user already picked.
"""
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from ..services.access import module_access_required, module_disabled_response
from ..services.modules import REPORT_MODULES, report_enabled
from ..services.reporting import (
    REPORTS_BY_SLUG,
    parse_report_range,
    report_csv_response,
    report_filename,
    report_shell_context,
    report_table_rows,
    report_total_cells,
)

REPORT_PAGE_SIZE = 50


@module_access_required("reports")
def analytics_report(request, slug):
    spec = REPORTS_BY_SLUG.get(slug)
    if spec is None:
        raise Http404("Unknown report.")
    if not report_enabled(slug):
        return module_disabled_response(request, REPORT_MODULES[slug])

    today = timezone.localdate()
    if spec.range_mode == "as_of":
        _, end = parse_report_range(request.GET, None, today)
        start, end = None, end or today
    else:
        start, end = parse_report_range(request.GET, today.replace(day=1), today)

    report = spec.builder(start, end, request.GET)
    if request.GET.get("export") == "csv":
        return report_csv_response(report, report_filename(spec.slug, start, end))

    page = Paginator(report.rows, REPORT_PAGE_SIZE).get_page(request.GET.get("page"))
    extra_params = {item["name"]: item["value"] for item in report.filters}
    context = report_shell_context(
        request, spec.slug, start=start, end=end, range_mode=spec.range_mode, extra_params=extra_params
    )
    context.update(
        {
            "report_title": spec.title,
            "report_description": spec.description,
            "report_icon": spec.icon,
            "report": report,
            "columns": report.columns,
            "table_rows": report_table_rows(report, page.object_list),
            "total_cells": report_total_cells(report),
            "page_obj": page,
        }
    )
    return render(request, "pos/reports/table_report.html", context)
