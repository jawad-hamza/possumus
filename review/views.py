import json

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts.services.report_utils import csv_response
from pos.services.access import admin_required
from pos.services.throttling import allow_action

from . import services
from .models import ReviewNote, ReviewSwitch

STATUS_FILTERS = (
    ("open", "Open"),
    ("resolved", "Resolved"),
    ("all", "All"),
)
PAGE_SIZE = 50


def _json(body, status=200):
    response = JsonResponse(body, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _signed_out():
    return _json({"error": "Sign in again to continue.", "redirect": reverse("login")}, status=401)


def _throttled(request, scope, budget):
    limit, window = budget
    return not allow_action(f"review-{scope}", request.user.pk, limit=limit, window_seconds=window)


def _read_json(request):
    if len(request.body or b"") > services.BODY_MAX_BYTES:
        return None, _json({"error": "That note is too long."}, status=413)
    try:
        return json.loads(request.body or b"{}"), None
    except (TypeError, ValueError):
        return None, _json({"error": "Invalid note."}, status=400)


def _set_resolved(note, resolved, user):
    note.resolved = resolved
    note.resolved_by = user if resolved else None
    note.resolved_at = timezone.now() if resolved else None
    note.save(update_fields=["resolved", "resolved_by", "resolved_at"])


@require_http_methods(["GET", "POST"])
def notes_api(request):
    """GET: the open notes of one screen (?path=&query=). POST: pin a new note."""
    if not request.user.is_authenticated:
        return _signed_out()
    if not services.tool_enabled():
        return _json({"error": "The comment tool is switched off."}, status=403)

    if request.method == "GET":
        if _throttled(request, "read", services.READ_LIMIT):
            return _json({"error": "Too many requests. Wait a minute."}, status=429)
        path = services.clean_path(request.GET.get("path"))
        if path is None:
            return _json({"error": "Unknown screen."}, status=400)
        notes = (
            ReviewNote.objects.filter(
                page_path=path,
                page_query=services.normalize_query(request.GET.get("query")),
                resolved=False,
            )
            .select_related("resolved_by")
            .order_by("created_at", "id")
        )
        manage = services.can_manage_notes(request.user)
        return _json({"notes": [services.serialize_note(note, request.user, manage) for note in notes]})

    payload, error = _read_json(request)
    if error is not None:
        return error
    fields, message = services.validate_note(payload, request.user)
    if message:
        return _json({"error": message}, status=400)
    if _throttled(request, "create", services.CREATE_LIMIT):
        return _json({"error": "Too many notes in a short time. Wait a few minutes."}, status=429)
    note = ReviewNote.objects.create(user=request.user, **fields)
    return _json({"note": services.serialize_note(note, request.user)}, status=201)


@require_GET
def note_api(request, pk):
    """One note, resolved or not: a link from the review page opens it on its screen."""
    if not request.user.is_authenticated:
        return _signed_out()
    note = ReviewNote.objects.select_related("resolved_by").filter(pk=pk).first()
    if note is None:
        return _json({"error": "That note no longer exists."}, status=404)
    return _json({"note": services.serialize_note(note, request.user)})


@require_POST
def note_resolve_api(request, pk):
    """Body {"resolved": true|false}. Admins, the owner and the note's own author may do this."""
    if not request.user.is_authenticated:
        return _signed_out()
    note = ReviewNote.objects.select_related("resolved_by").filter(pk=pk).first()
    if note is None:
        return _json({"error": "That note no longer exists."}, status=404)
    if not services.can_resolve(request.user, note):
        return _json({"error": "Only an admin or the person who wrote the note can resolve it."}, status=403)
    payload, error = _read_json(request)
    if error is not None:
        return error
    if _throttled(request, "resolve", services.RESOLVE_LIMIT):
        return _json({"error": "Too many requests. Wait a few minutes."}, status=429)
    resolved = payload.get("resolved", True) if isinstance(payload, dict) else True
    _set_resolved(note, bool(resolved), request.user)
    return _json({"note": services.serialize_note(note, request.user)})


def _status_from(request):
    status = request.GET.get("status") or "open"
    return status if status in dict(STATUS_FILTERS) else "open"


def _filtered(status):
    notes = ReviewNote.objects.select_related("user", "resolved_by").order_by("-created_at", "-id")
    if status == "open":
        return notes.filter(resolved=False)
    if status == "resolved":
        return notes.filter(resolved=True)
    return notes


def _export_csv(notes):
    tz = timezone.get_current_timezone()
    rows = []
    for note in notes:
        rows.append(
            [
                note.id,
                timezone.localtime(note.created_at, tz).strftime("%Y-%m-%d %H:%M"),
                note.author_name,
                note.screen_label,
                note.page_url,
                (note.anchor or {}).get("hint", ""),
                note.text,
                "Resolved" if note.resolved else "Open",
                services.display_name(note.resolved_by) if note.resolved_by_id else "",
                timezone.localtime(note.resolved_at, tz).strftime("%Y-%m-%d %H:%M") if note.resolved_at else "",
            ]
        )
    header = ["ID", "Written", "Author", "Screen", "Page", "Spot", "Note", "Status", "Resolved by", "Resolved at"]
    return csv_response("review-notes", header, rows)


def _back_to_list(request):
    target = request.POST.get("next") or ""
    if target.startswith(reverse("review_notes")):
        return redirect(target)
    return redirect("review_notes")


@admin_required
@require_http_methods(["GET", "POST"])
def review_notes(request):
    """Every note, newest first, for the owner and admins: open each spot, resolve, export."""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "switch":
            mode = request.POST.get("mode")
            if mode not in dict(ReviewSwitch.MODE_CHOICES):
                messages.error(request, "Pick On, Off or Follow the server setting.")
            else:
                ReviewSwitch.set_mode(mode, request.user)
                enabled, _ = services.tool_state()
                messages.success(request, "The comment tool is now " + ("on." if enabled else "off."))
            return _back_to_list(request)
        if action in {"resolve", "reopen"}:
            note_id = request.POST.get("note") or ""
            note = get_object_or_404(ReviewNote, pk=int(note_id) if note_id.isdigit() else 0)
            _set_resolved(note, action == "resolve", request.user)
            messages.success(request, "Note resolved." if action == "resolve" else "Note reopened.")
            return _back_to_list(request)
        messages.error(request, "Unknown action.")
        return _back_to_list(request)

    status = _status_from(request)
    notes = _filtered(status)
    if request.GET.get("format") == "csv":
        return _export_csv(notes)

    counts = ReviewNote.objects.aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(resolved=False)),
        resolved=Count("id", filter=Q(resolved=True)),
    )
    count_for = {"open": counts["open"], "resolved": counts["resolved"], "all": counts["total"]}
    page_obj = Paginator(notes, PAGE_SIZE).get_page(request.GET.get("page"))
    enabled, mode = services.tool_state()
    return render(
        request,
        "review/notes.html",
        {
            "page_obj": page_obj,
            "notes": page_obj.object_list,
            "status": status,
            "status_filters": [(value, label, count_for[value]) for value, label in STATUS_FILTERS],
            "page_querystring": f"status={status}",
            "tool_enabled": enabled,
            "tool_mode": mode,
            "mode_choices": ReviewSwitch.MODE_CHOICES,
            "server_default": services.server_default_enabled(),
        },
    )
