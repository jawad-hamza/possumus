"""Landing, signup, verification, Google sign-in, login/logout, password recovery and first-run setup."""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.tokens import default_token_generator
from django.contrib.auth.views import (
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.encoding import force_bytes, force_str
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST

from accounts.services.audit import record_audit
from accounts.services.chart_of_accounts import ensure_default_accounts
from ..forms import (
    AccountPasswordChangeForm,
    AccountPasswordResetForm,
    AccountSetPasswordForm,
    LoginForm,
    OwnerBusinessSetupForm,
    OwnerSignupForm,
)
from ..models import CompanyProfile, OwnerOnboarding, trial_days
from ..services.access import (
    ADMIN_ROLE,
    assign_role,
    close_user_session,
    default_home_url,
    ensure_role_groups,
    get_owner_onboarding,
    is_first_user_setup_required,
    open_user_session,
)
from ..services.account_tokens import password_reset_token_generator
from ..services.billing import google_sign_in_setup_message
from ..services.google_auth import (
    build_google_authorization_url,
    clear_google_oauth_session,
    desktop_mode,
    exchange_google_authorization_code,
    find_user_by_google_sub,
    get_google_oauth_session,
    google_sign_in_enabled,
    remember_google_sub,
    stored_google_sub,
    verify_google_identity_token,
)
from ..services.throttling import (
    LOGIN_SCOPE,
    PASSWORD_RESET_EMAIL_SCOPE,
    PASSWORD_RESET_IP_SCOPE,
    RESEND_EMAIL_SCOPE,
    RESEND_IP_SCOPE,
    SIGNUP_IP_SCOPE,
    allow_action,
    clear_attempts,
    clear_login_failures,
    client_ip,
    lockout_message,
    login_lock_seconds_remaining,
    register_login_failure,
)
from ._shared import (
    LANDING_BRAND_NAME,
    LANDING_CHECKOUT_PROVIDER,
    LANDING_PUBLIC_DOMAIN,
    LANDING_SUPPORT_EMAIL,
    _landing_plan_lookup,
    _landing_pricing_plans,
    _remember_browser_time_zone,
)

logger = logging.getLogger(__name__)

RESEND_EMAIL_LIMIT = 3
RESEND_IP_LIMIT = 10
PASSWORD_RESET_EMAIL_LIMIT = 3
PASSWORD_RESET_IP_LIMIT = 10
SIGNUP_IP_LIMIT = 10
PUBLIC_ACTION_WINDOW_SECONDS = 3600
RESEND_GENERIC_MESSAGE = (
    "If that address has a verification pending, a fresh link is on its way. "
    "Give it a few minutes and check the spam folder before trying again."
)


def _selected_plan_code(plan_code):
    plan = _landing_plan_lookup(plan_code)
    return plan["code"] if plan else OwnerOnboarding.PLAN_STARTER


def _google_sign_in_url(*, plan_code="", next_url=""):
    params = {}
    if plan_code:
        params["plan"] = _selected_plan_code(plan_code)
    if next_url:
        params["next"] = next_url
    base_url = reverse("google_auth_start")
    return f"{base_url}?{urlencode(params)}" if params else base_url


def _signup_url_for_plan(plan_code):
    return f"{reverse('signup')}?{urlencode({'plan': _selected_plan_code(plan_code)})}"


def _public_signup_is_open():
    return is_first_user_setup_required()


def _discard_stale_owner_signups():
    """Drop never-verified owner signups so a new registration can take their place."""
    get_user_model().objects.filter(
        is_active=False,
        last_login__isnull=True,
        owner_onboarding__email_verified=False,
        owner_onboarding__business_created_at__isnull=True,
    ).delete()


def _public_signup_lock_message():
    if OwnerOnboarding.objects.filter(email_verified=False, business_created_at__isnull=True).exists():
        return "A registration is already in progress for this workspace. Verify that email address and then sign in to continue."
    return "This ZehnMS workspace already has an owner account. Sign in with the existing owner email or a team login."


def _owner_verification_url(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("verify_signup_email", args=[uid, token]))


def _send_owner_verification_email(request, onboarding):
    verification_url = _owner_verification_url(request, onboarding.user)
    subject = f"{LANDING_BRAND_NAME} email verification"
    body = (
        f"Welcome to {LANDING_BRAND_NAME}.\n\n"
        f"Verify your email to unlock your {trial_days()} free days on the {onboarding.plan_label} package and create the business workspace.\n\n"
        "You can purchase or upgrade plans later after the workspace is live.\n\n"
        f"Verification link:\n{verification_url}\n\n"
        "If you did not request this account, you can ignore this email."
    )
    send_mail(subject, body, LANDING_SUPPORT_EMAIL, [onboarding.user.email], fail_silently=False)
    return verification_url


def _try_send_owner_verification_email(request, onboarding):
    """Send the verification email without letting a mail outage become a 500.

    Returns (sent, verification_url). The account is kept either way so the owner
    can simply press Resend once mail is working again.
    """
    try:
        verification_url = _send_owner_verification_email(request, onboarding)
    except Exception:
        logger.exception("Owner verification email could not be sent.")
        return False, _owner_verification_url(request, onboarding.user)
    onboarding.mark_verification_sent()
    return True, verification_url


def _email_is_taken(email):
    return get_user_model().objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists()


def _render_verification_sent(request, *, email, plan_code="", verification_url="", mail_failed=False):
    # Everything shown here comes from what the visitor typed, never from a stored
    # account, so the page cannot tell a real pending signup from an unknown email.
    return render(
        request,
        "pos/signup_email_sent.html",
        {
            "brand_name": LANDING_BRAND_NAME,
            "email": email,
            "plan": _landing_plan_lookup(plan_code) if plan_code else None,
            "login_url": reverse("login"),
            "resend_email": email,
            "mail_failed": mail_failed,
            "debug_verification_url": verification_url if settings.DEBUG else "",
        },
    )


def _activate_owner_trial_if_needed(user):
    onboarding = get_owner_onboarding(user)
    if onboarding and onboarding.email_verified:
        onboarding.start_trial()
    return onboarding


def _bool_claim(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _google_safe_next_url(request, next_url):
    next_url = (next_url or "").strip()
    if not next_url:
        return ""
    return (
        next_url
        if url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        else ""
    )


def _google_failure_redirect():
    return "signup" if _public_signup_is_open() else "login"


def _find_user_by_email_or_username(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    user_model = get_user_model()
    return (
        user_model.objects.filter(Q(email__iexact=identifier) | Q(username__iexact=identifier))
        .order_by("id")
        .first()
    )


def _update_user_from_google_claims(user, claims):
    email = (claims.get("email") or "").strip().lower()
    given_name = (claims.get("given_name") or "").strip()
    family_name = (claims.get("family_name") or "").strip()
    update_fields = []

    if email and not user.email:
        user.email = email
        update_fields.append("email")
    if given_name and not user.first_name:
        user.first_name = given_name
        update_fields.append("first_name")
    if family_name and not user.last_name:
        user.last_name = family_name
        update_fields.append("last_name")

    if update_fields:
        user.save(update_fields=update_fields)


def _resolve_google_user(request, claims, plan_code):
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise ValueError("Google did not return an email address for this account.")
    if not _bool_claim(claims.get("email_verified")):
        raise ValueError("Google did not confirm that email address. Use a verified Google account instead.")

    google_sub = str(claims.get("sub") or "").strip()
    # Prefer the permanent Google account id once we have seen it; the email match is
    # only the first-time link.
    user = find_user_by_google_sub(google_sub) or _find_user_by_email_or_username(email)
    now = timezone.now()

    if user is None:
        if not _public_signup_is_open():
            raise ValueError("This Google account is not registered for the current ZehnMS workspace.")

        user_model = get_user_model()
        with transaction.atomic():
            _discard_stale_owner_signups()
            user = user_model(
                username=email,
                email=email,
                first_name=(claims.get("given_name") or "").strip(),
                last_name=(claims.get("family_name") or "").strip(),
                is_active=True,
                is_staff=False,
            )
            user.set_unusable_password()
            user.save()
            onboarding = OwnerOnboarding.objects.create(
                user=user,
                selected_plan=_selected_plan_code(plan_code),
            )
            onboarding.mark_email_verified(now=now)
            onboarding.start_trial(now=now)
            remember_google_sub(user, google_sub)
            record_audit(
                action="owner_google_signup_created",
                description=f"Owner account '{email}' created through Google sign-in.",
                actor=user,
                target=user,
                metadata={"plan": onboarding.plan_label},
            )
        return user, onboarding, True

    known_sub = stored_google_sub(user)
    if known_sub and google_sub and known_sub != google_sub:
        # Same email, different Google account: the address was recycled or reassigned.
        raise ValueError("This Google account is not registered for the current ZehnMS workspace.")

    onboarding = getattr(user, "owner_onboarding", None)
    if not user.is_active and (onboarding is None or onboarding.email_verified):
        raise ValueError("This Google account is not registered for the current ZehnMS workspace.")

    _update_user_from_google_claims(user, claims)
    if onboarding is not None:
        if not onboarding.email_verified:
            # The password on a never-verified signup may have been chosen by someone
            # else using this address, so Google becomes the only way in.
            user.set_unusable_password()
            user.is_active = True
            user.save(update_fields=["password", "is_active"])
            onboarding.mark_email_verified(now=now)
        onboarding.start_trial(now=now)
    remember_google_sub(user, google_sub)

    return user, onboarding, False


def _sign_in_user_and_redirect(request, user, *, next_url=""):
    login(request, user)
    open_user_session(request, user)
    onboarding = _activate_owner_trial_if_needed(user)
    welcome_name = user.get_full_name() or user.email or user.username
    home_url = default_home_url(user)
    messages.success(request, f"Welcome back, {welcome_name}.")
    if onboarding and onboarding.email_verified and not onboarding.business_created_at:
        messages.info(request, "Create the business workspace to finish onboarding.")
    safe_next_url = _google_safe_next_url(request, next_url)
    if home_url != reverse("setup_admin") and safe_next_url:
        return redirect(safe_next_url)
    return redirect(home_url)


def _landing_context():
    pricing_plans = []
    for plan in _landing_pricing_plans():
        pricing_plans.append(
            {
                **plan,
                "cta_url": reverse("landing_plan_checkout", kwargs={"plan_code": plan["code"]}),
            }
        )
    return {
        "brand_name": LANDING_BRAND_NAME,
        "public_domain": LANDING_PUBLIC_DOMAIN,
        "support_email": LANDING_SUPPORT_EMAIL,
        "checkout_provider": LANDING_CHECKOUT_PROVIDER,
        "pricing_plans": pricing_plans,
    }


def _desktop_entry_redirect():
    return redirect("signup" if _public_signup_is_open() else "login")


def home(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if desktop_mode():
        # An installed shop has no marketing site: go straight to work.
        return _desktop_entry_redirect()

    return render(
        request,
        "pos/landing.html",
        {
            **_landing_context(),
            "trial_cta_url": reverse("start_trial"),
            "login_url": reverse("login"),
            "signup_url": "#pricing",
        },
    )


def landing_plan_checkout(request, plan_code):
    plan = _landing_plan_lookup(plan_code)
    if plan is None:
        raise Http404("Unknown plan.")

    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if desktop_mode():
        return _desktop_entry_redirect()

    if _public_signup_is_open():
        messages.info(
            request,
            f"{plan['name']} selected. Create your owner account and verify your email to unlock {trial_days()} free days.",
        )
    return redirect(_signup_url_for_plan(plan["code"]))


def start_trial(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if desktop_mode():
        return _desktop_entry_redirect()

    return redirect(_signup_url_for_plan(OwnerOnboarding.PLAN_STARTER))


def _complete_desktop_signup(request, form):
    """Offline install: there is no mailbox to verify, so the owner is signed in at once."""
    email = form.cleaned_data["email"]
    with transaction.atomic():
        _discard_stale_owner_signups()
        if _email_is_taken(email):
            form.add_error("email", "That email belongs to a disabled account on this computer. Use a different one.")
            return None
        user = form.save(commit=False)
        user.is_active = True
        user.save()
        now = timezone.now()
        onboarding = OwnerOnboarding.objects.create(user=user, selected_plan=form.cleaned_data["selected_plan"])
        onboarding.mark_email_verified(now=now)
        onboarding.start_trial(now=now)
        record_audit(
            action="owner_desktop_signup_created",
            description=f"Owner account '{email}' created on this installation.",
            actor=user,
            target=user,
        )

    login(request, user)
    open_user_session(request, user)
    messages.success(request, "Owner account created. Add your business details to open the workspace.")
    return redirect("setup_admin")


def _complete_cloud_signup(request, form):
    email = form.cleaned_data["email"]
    plan_code = form.cleaned_data["selected_plan"]
    onboarding = None
    with transaction.atomic():
        _discard_stale_owner_signups()
        if not _email_is_taken(email):
            user = form.save()
            onboarding = OwnerOnboarding.objects.create(user=user, selected_plan=plan_code)

    if onboarding is None:
        # Already registered: answer exactly as for a new address and send nothing.
        return _render_verification_sent(request, email=email, plan_code=plan_code)

    sent, verification_url = _try_send_owner_verification_email(request, onboarding)
    return _render_verification_sent(
        request,
        email=email,
        plan_code=plan_code,
        verification_url=verification_url,
        mail_failed=not sent,
    )


def signup_view(request):
    ensure_role_groups()
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    is_desktop = desktop_mode()
    selected_plan = _selected_plan_code(request.POST.get("selected_plan") or request.GET.get("plan"))
    selected_plan_data = _landing_plan_lookup(selected_plan)
    form = OwnerSignupForm(request.POST or None, initial={"selected_plan": selected_plan})
    form.fields["selected_plan"].initial = selected_plan
    signup_locked = not _public_signup_is_open()
    signup_lock_message = _public_signup_lock_message() if signup_locked else ""

    if request.method == "POST":
        if signup_locked:
            messages.info(request, signup_lock_message)
        elif not allow_action(
            SIGNUP_IP_SCOPE, client_ip(request), limit=SIGNUP_IP_LIMIT, window_seconds=PUBLIC_ACTION_WINDOW_SECONDS
        ):
            messages.error(request, "Too many signup attempts from this network. Wait a while and try again.")
        elif form.is_valid():
            response = _complete_desktop_signup(request, form) if is_desktop else _complete_cloud_signup(request, form)
            if response is not None:
                return response
            messages.error(request, "Please review the owner account details below.")
        else:
            messages.error(request, "Please review the owner account details below.")

    return render(
        request,
        "pos/signup.html",
        {
            "brand_name": LANDING_BRAND_NAME,
            "form": form,
            "plan": selected_plan_data,
            "login_url": reverse("login"),
            "desktop_mode": is_desktop,
            "google_sign_in_url": _google_sign_in_url(plan_code=selected_plan),
            "google_sign_in_enabled": google_sign_in_enabled(),
            "google_sign_in_setup_message": google_sign_in_setup_message(),
            "signup_locked": signup_locked,
            "signup_lock_message": signup_lock_message,
        },
    )


@require_POST
def resend_verification_email(request):
    if desktop_mode():
        return redirect("login")

    email = (request.POST.get("email") or "").strip().lower()
    if not email:
        messages.error(request, "Enter the owner email address first.")
        return redirect("login")

    # Both budgets are keyed by what the visitor typed, never by whether the account
    # exists, so hitting the limit reveals nothing either.
    ip_allowed = allow_action(
        RESEND_IP_SCOPE, client_ip(request), limit=RESEND_IP_LIMIT, window_seconds=PUBLIC_ACTION_WINDOW_SECONDS
    )
    email_allowed = allow_action(
        RESEND_EMAIL_SCOPE, email, limit=RESEND_EMAIL_LIMIT, window_seconds=PUBLIC_ACTION_WINDOW_SECONDS
    )
    if not (ip_allowed and email_allowed):
        messages.error(request, "Too many verification emails were requested. Wait an hour before asking again.")
        return _render_verification_sent(request, email=email)

    user = get_user_model().objects.filter(email__iexact=email).order_by("id").first()
    onboarding = getattr(user, "owner_onboarding", None) if user is not None else None
    verification_url = ""
    if user is not None and onboarding is not None and not onboarding.email_verified and not user.is_active:
        _sent, verification_url = _try_send_owner_verification_email(request, onboarding)

    messages.info(request, RESEND_GENERIC_MESSAGE)
    return _render_verification_sent(request, email=email, verification_url=verification_url)


def verify_signup_email(request, uidb64, token):
    user_model = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = user_model.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, user_model.DoesNotExist):
        user = None

    onboarding = getattr(user, "owner_onboarding", None) if user is not None else None
    if user is None or onboarding is None:
        messages.error(request, "That verification link is invalid.")
        return redirect("login")

    if onboarding.email_verified:
        messages.info(request, "Your email is already verified. Sign in to continue.")
        if request.user.is_authenticated and request.user.pk == user.pk:
            return redirect(default_home_url(user))
        return redirect("login")

    if not default_token_generator.check_token(user, token):
        messages.error(request, "That verification link is no longer valid. Use the resend option to get a fresh email.")
        return redirect("login")

    if not _public_signup_is_open():
        # Someone else finished onboarding first; a leftover link must not mint a second owner.
        messages.error(request, "That verification link is no longer valid. Sign in with the workspace owner account.")
        return redirect("login")

    with transaction.atomic():
        user.is_active = True
        user.save(update_fields=["is_active"])
        onboarding.mark_email_verified()
        onboarding.start_trial()

    login(request, user)
    open_user_session(request, user)
    messages.success(request, "Email verified. Finish creating your business workspace.")
    return redirect("setup_admin")


def google_auth_start(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if desktop_mode():
        return _desktop_entry_redirect()

    _remember_browser_time_zone(request, request.GET.get("browser_time_zone"))
    if not google_sign_in_enabled():
        messages.info(
            request,
            google_sign_in_setup_message(),
        )
        return redirect(_google_failure_redirect())

    plan_code = _selected_plan_code(request.GET.get("plan")) if _public_signup_is_open() else ""
    next_url = _google_safe_next_url(request, request.GET.get("next"))
    return redirect(build_google_authorization_url(request, plan_code=plan_code, next_url=next_url))


def google_auth_callback(request):
    if request.user.is_authenticated:
        return redirect(default_home_url(request.user))

    if desktop_mode():
        return _desktop_entry_redirect()

    if not google_sign_in_enabled():
        messages.info(
            request,
            google_sign_in_setup_message(),
        )
        return redirect(_google_failure_redirect())

    if request.GET.get("error"):
        clear_google_oauth_session(request)
        messages.error(request, "Google sign-in was cancelled before completion.")
        return redirect(_google_failure_redirect())

    oauth_session = get_google_oauth_session(request)
    state = (request.GET.get("state") or "").strip()
    code = (request.GET.get("code") or "").strip()
    if not oauth_session.get("state") or state != oauth_session.get("state"):
        clear_google_oauth_session(request)
        messages.error(request, "Google sign-in expired or could not be verified. Please try again.")
        return redirect(_google_failure_redirect())
    if not code:
        clear_google_oauth_session(request)
        messages.error(request, "Google sign-in did not return an authorization code.")
        return redirect(_google_failure_redirect())

    try:
        token_payload = exchange_google_authorization_code(request, code)
        claims = verify_google_identity_token(
            token_payload.get("id_token"),
            expected_nonce=oauth_session.get("nonce", ""),
        )
        user, onboarding, created_owner = _resolve_google_user(
            request,
            claims,
            oauth_session.get("plan_code", ""),
        )
    except ValueError as exc:
        clear_google_oauth_session(request)
        messages.error(request, str(exc))
        return redirect(_google_failure_redirect())

    clear_google_oauth_session(request)
    if created_owner:
        messages.success(request, "Google account verified. Finish creating the business workspace to continue.")
    elif onboarding and onboarding.email_verified and not onboarding.business_created_at:
        messages.success(request, "Google account verified. Continue with business setup.")
    return _sign_in_user_and_redirect(request, user, next_url=oauth_session.get("next_url", ""))


def setup_admin(request):
    ensure_role_groups()
    if not request.user.is_authenticated:
        messages.info(request, "Sign in with your registered owner account to create the business workspace.")
        return redirect("login")

    onboarding = get_owner_onboarding(request.user)
    if onboarding is None:
        if _public_signup_is_open():
            return redirect("signup")
        messages.error(request, "Only the registered owner account can create the business workspace.")
        return redirect(default_home_url(request.user))

    if not onboarding.email_verified:
        messages.warning(request, "Verify your email before creating the business workspace.")
        return redirect("login")

    if onboarding.business_created_at or CompanyProfile.objects.filter(is_active=True).exists():
        return redirect(default_home_url(request.user))

    company_profile = CompanyProfile(is_active=True, email=request.user.email or "")
    form = OwnerBusinessSetupForm(request.POST or None, instance=company_profile)
    if request.method == "POST":
        _remember_browser_time_zone(request, request.POST.get("browser_time_zone"))
        if form.is_valid():
            with transaction.atomic():
                company = form.save(commit=False)
                company.is_active = True
                company.save()
                ensure_default_accounts()
                assign_role(request.user, ADMIN_ROLE)
                _activate_owner_trial_if_needed(request.user)
                onboarding.mark_business_created()
                record_audit(
                    action="business_workspace_created",
                    description=f"Business workspace '{company.name}' created during owner onboarding.",
                    actor=request.user,
                    target=company,
                    metadata={"plan": onboarding.plan_label},
                )
            messages.success(request, "Business workspace created. Your owner account now has full control.")
            return redirect(default_home_url(request.user))
        messages.error(request, "Please review the business details below.")

    return render(
        request,
        "pos/setup_admin.html",
        {
            "form": form,
            "onboarding": onboarding,
            "brand_name": LANDING_BRAND_NAME,
            "desktop_mode": desktop_mode(),
            "google_sign_in_enabled": google_sign_in_enabled(),
        },
    )


def _record_failed_login(request, form, identifier, *, locked_now):
    matched_user = form._find_user(identifier)
    metadata = {"identifier": identifier[:150], "ip_address": client_ip(request)}
    record_audit(
        action="login_failed",
        description=f"Failed sign-in attempt for '{identifier[:150]}'.",
        target=matched_user,
        metadata=metadata,
    )
    if locked_now:
        record_audit(
            action="login_locked_out",
            description=f"Sign-in paused for '{identifier[:150]}' after repeated failed attempts.",
            target=matched_user,
            metadata=metadata,
        )


def login_view(request):
    ensure_role_groups()
    if request.user.is_authenticated:
        home_url = default_home_url(request.user)
        if home_url != reverse("login"):
            return redirect(home_url)
        # Every area this role could open is switched off (or it has none): redirecting would loop forever.
        close_user_session(request)
        logout(request)
        messages.error(request, "Your account has no area it can open right now. Ask the owner to check your role.")
        return redirect("login")

    if desktop_mode() and _public_signup_is_open():
        # Fresh install: nobody can sign in yet, so start with the owner account.
        return redirect("signup")

    next_url = request.GET.get("next") or request.POST.get("next") or ""
    form = LoginForm(request, data=request.POST or None)
    pending_verification_email = ""

    if request.method == "POST":
        _remember_browser_time_zone(request, request.POST.get("browser_time_zone"))
        identifier = (request.POST.get("username") or "").strip()
        has_credentials = bool(identifier and request.POST.get("password"))
        lock_seconds = login_lock_seconds_remaining(request, identifier)

        if lock_seconds:
            # Checked before the password so a locked-out attacker learns nothing more.
            form = LoginForm(request, initial={"username": identifier})
            messages.error(request, lockout_message(lock_seconds))
        elif form.is_valid():
            clear_login_failures(request, identifier)
            return _sign_in_user_and_redirect(request, form.get_user(), next_url=next_url)
        elif form.pending_verification_user is not None:
            pending_verification_email = form.pending_verification_user.email
        elif has_credentials:
            locked_now = register_login_failure(request, identifier)
            _record_failed_login(request, form, identifier, locked_now=locked_now)
            if locked_now:
                messages.error(request, lockout_message(login_lock_seconds_remaining(request, identifier)))
            else:
                messages.error(request, "Check the email or login ID and password and try again.")
        else:
            messages.error(request, "Enter both your email or login ID and your password.")

    is_desktop = desktop_mode()
    return render(
        request,
        "pos/login.html",
        {
            "form": form,
            "next_url": next_url,
            "desktop_mode": is_desktop,
            "pending_verification_email": pending_verification_email,
            "signup_url": reverse("signup") if _public_signup_is_open() else "",
            "show_public_signup": _public_signup_is_open(),
            "google_sign_in_url": _google_sign_in_url(next_url=next_url),
            "google_sign_in_enabled": google_sign_in_enabled(),
            "google_sign_in_setup_message": google_sign_in_setup_message(),
        },
    )


class _PasswordResetRequestView(PasswordResetView):
    template_name = "pos/password_reset_form.html"
    email_template_name = "pos/email/password_reset_email.txt"
    subject_template_name = "pos/email/password_reset_subject.txt"
    form_class = AccountPasswordResetForm
    token_generator = password_reset_token_generator
    success_url = reverse_lazy("password_reset_done")
    from_email = LANDING_SUPPORT_EMAIL
    extra_email_context = {"brand_name": LANDING_BRAND_NAME}

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("password_change")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({"brand_name": LANDING_BRAND_NAME, "desktop_mode": desktop_mode()})
        return context

    def post(self, request, *args, **kwargs):
        if desktop_mode():
            # No mail server on an offline install; the page explains the local recovery path.
            return self.get(request, *args, **kwargs)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        email = (form.cleaned_data.get("email") or "").strip().lower()
        ip_allowed = allow_action(
            PASSWORD_RESET_IP_SCOPE,
            client_ip(self.request),
            limit=PASSWORD_RESET_IP_LIMIT,
            window_seconds=PUBLIC_ACTION_WINDOW_SECONDS,
        )
        email_allowed = allow_action(
            PASSWORD_RESET_EMAIL_SCOPE,
            email,
            limit=PASSWORD_RESET_EMAIL_LIMIT,
            window_seconds=PUBLIC_ACTION_WINDOW_SECONDS,
        )
        if not (ip_allowed and email_allowed):
            # Same destination as a real send, so the limit cannot be used as an oracle.
            return HttpResponseRedirect(self.get_success_url())
        return super().form_valid(form)


class _PasswordResetDoneView(PasswordResetDoneView):
    template_name = "pos/password_reset_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["brand_name"] = LANDING_BRAND_NAME
        return context


class _PasswordResetConfirmView(PasswordResetConfirmView):
    template_name = "pos/password_reset_confirm.html"
    form_class = AccountSetPasswordForm
    token_generator = password_reset_token_generator
    # Signing the owner straight in avoids a second trip through the login form.
    post_reset_login = True
    success_url = reverse_lazy("home")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["brand_name"] = LANDING_BRAND_NAME
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.user
        for identifier in {user.get_username(), user.email}:
            if identifier:
                clear_attempts(LOGIN_SCOPE, f"{identifier.strip().lower()}|{client_ip(self.request)}")
        if self.request.user.is_authenticated:
            open_user_session(self.request, user)
        record_audit(
            action="password_reset_completed",
            description=f"Password reset through the email link for {user.get_full_name() or user.get_username()}.",
            actor=user,
            target=user,
            metadata={"ip_address": client_ip(self.request)},
        )
        messages.success(self.request, "Your password was changed and you are signed in.")
        return response


class _PasswordChangeView(PasswordChangeView):
    template_name = "pos/password_change.html"
    success_url = reverse_lazy("home")

    def get_form_class(self):
        # Accounts created through Google have no current password to confirm.
        if not self.request.user.has_usable_password():
            return AccountSetPasswordForm
        return AccountPasswordChangeForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "page_title": "Change Password",
                "page_subtitle": "Update the password for the account you are signed in with.",
                "page_icon": "bi-shield-lock",
                "page_icon_accent": "slate",
                "has_current_password": self.request.user.has_usable_password(),
                "cancel_url": default_home_url(self.request.user),
            }
        )
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        record_audit(
            action="password_changed",
            description=f"Password changed by {self.request.user.get_full_name() or self.request.user.get_username()}.",
            actor=self.request.user,
            target=self.request.user,
            metadata={"ip_address": client_ip(self.request)},
        )
        messages.success(self.request, "Password updated. Other devices signed in with the old password were signed out.")
        return response


password_reset_request = _PasswordResetRequestView.as_view()
password_reset_done = _PasswordResetDoneView.as_view()
password_reset_confirm = _PasswordResetConfirmView.as_view()
password_change = _PasswordChangeView.as_view()


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        close_user_session(request)
        logout(request)
    return redirect("login")


@require_POST
def set_client_time_zone(request):
    time_zone_name = _remember_browser_time_zone(
        request,
        request.POST.get("time_zone") or request.POST.get("browser_time_zone"),
    )
    if not time_zone_name:
        return JsonResponse({"ok": False, "error": "invalid_time_zone"}, status=400)
    return JsonResponse({"ok": True, "time_zone": time_zone_name})
