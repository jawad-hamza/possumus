from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

POS_SELECTED_CUSTOMER_KEY = "pos_selected_customer_id"
POS_PREFILL_CREDIT_KEY = "pos_prefill_credit"


def safe_next_url(request):
    """The posted or ?next= address when it stays on this site, otherwise "".

    Only paths on this site are accepted ("/products/?page=2"), never another host,
    a scheme-relative "//host" or a "javascript:" address.
    """
    candidate = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith(("//", "/\\")):
        return ""
    if not url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return ""
    return candidate


def get_safe_next_url(request, default_name):
    return safe_next_url(request) or reverse(default_name)


def return_context(request, default_url):
    """Template values for a page that must send the user back where he came from.

    next_url goes into a hidden "next" field; back_url is what Save, Cancel and Back lead to.
    """
    next_url = safe_next_url(request)
    return {"next_url": next_url, "back_url": next_url or default_url}


def with_query(url, **params):
    """url with the given query parameters set (a value of None removes one); the fragment is kept."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in params]
    query.extend((key, str(value)) for key, value in params.items() if value not in (None, ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def with_touched_row(url, pk):
    """Marks the record just saved so the list it returns to can highlight that row."""
    if not pk or "#" in url:
        return url
    return f"{url}#touched-{pk}"


def remember_customer_for_pos(request, customer_id, *, credit=False):
    request.session[POS_SELECTED_CUSTOMER_KEY] = customer_id
    request.session[POS_PREFILL_CREDIT_KEY] = credit


def pop_customer_for_pos(request):
    return (
        request.session.pop(POS_SELECTED_CUSTOMER_KEY, None),
        bool(request.session.pop(POS_PREFILL_CREDIT_KEY, False)),
    )


def build_customer_add_url():
    next_url = f"{reverse('pos_screen')}?credit=1"
    return f"{reverse('customer_add')}?{urlencode({'next': next_url})}"


def build_supplier_add_url(return_name='purchase_add'):
    next_url = reverse(return_name)
    return f"{reverse('supplier_add')}?{urlencode({'next': next_url})}"
