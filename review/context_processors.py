from django.urls import reverse

from . import services


def review_tool(request):
    """The comment tool's settings for pos/base.html, or None when it must not show.

    Handed over as a callable so pages that never ask (sign-in, printouts) cost no query.
    """
    answer = {}

    def config():
        if "value" in answer:
            return answer["value"]
        user = getattr(request, "user", None)
        value = None
        if getattr(user, "is_authenticated", False) and services.tool_enabled():
            value = {
                "listUrl": reverse("review_notes_api"),
                "createUrl": reverse("review_notes_api"),
                "noteUrl": reverse("review_note_api", args=[0]),
                "resolveUrl": reverse("review_note_resolve_api", args=[0]),
                "reviewUrl": reverse("review_notes"),
                "author": services.display_name(user),
                "userId": user.pk,
                "canManage": services.can_manage_notes(user),
            }
        answer["value"] = value
        return value

    def state():
        enabled, mode = services.tool_state()
        return {"enabled": enabled, "mode": mode}

    return {"review_tool": config, "review_tool_state": state}
