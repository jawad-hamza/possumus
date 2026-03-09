from urllib.parse import urlencode

from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

POS_SELECTED_CUSTOMER_KEY = "pos_selected_customer_id"
POS_PREFILL_CREDIT_KEY = "pos_prefill_credit"


def get_safe_next_url(request, default_name):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return reverse(default_name)


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
