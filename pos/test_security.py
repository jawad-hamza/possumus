import base64
import json
import re
import smtplib
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.models import Account, AuditLog, BankAccount, Settlement
from pos.forms import TeamUserForm
from pos.models import Customer, OwnerOnboarding
from pos.services import throttling
from pos.services.access import ADMIN_ROLE, CASHIER_ROLE, assign_role, ensure_role_groups
from pos.services.account_tokens import password_reset_token_generator
from pos.services.google_auth import find_user_by_google_sub, google_sub_storage_available
from pos.test_support import AuthenticatedAdminTestCase

LOCMEM_EMAIL = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "StrongPass123!"


class CacheIsolatedTestCase(TestCase):
    """Throttle counters live in the cache, which outlives the per-test database rollback."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


def _make_admin(username="owner@example.com", password=PASSWORD, **extra):
    ensure_role_groups()
    user = get_user_model().objects.create_user(username=username, email=username, password=password, **extra)
    assign_role(user, ADMIN_ROLE)
    return user


def _pending_owner(email="pending@example.com", password=PASSWORD):
    user = get_user_model().objects.create_user(username=email, email=email, password=password, is_active=False)
    onboarding = OwnerOnboarding.objects.create(user=user, selected_plan=OwnerOnboarding.PLAN_STARTER)
    return user, onboarding


@override_settings(DEPLOYMENT_MODE="cloud", LOGIN_MAX_ATTEMPTS=3, LOGIN_LOCKOUT_SECONDS=600, TRUST_PROXY=False)
class LoginLockoutTests(CacheIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = _make_admin()

    def _login(self, password, username="owner@example.com", **extra):
        return self.client.post(reverse("login"), {"username": username, "password": password}, **extra)

    def test_repeated_failures_lock_the_account_even_for_the_right_password(self):
        for _ in range(3):
            response = self._login("wrong-password")
            self.assertEqual(response.status_code, 200)

        self.assertContains(response, "Too many failed sign-in attempts")
        locked_response = self._login(PASSWORD)

        self.assertEqual(locked_response.status_code, 200)
        self.assertContains(locked_response, "Too many failed sign-in attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_failed_attempts_and_lockout_are_audited(self):
        for _ in range(3):
            self._login("wrong-password")
        self._login("wrong-password")

        failures = AuditLog.objects.filter(action="login_failed")
        # The attempt made while locked is refused before the password is looked at.
        self.assertEqual(failures.count(), 3)
        self.assertEqual(failures.first().target_id, str(self.user.pk))
        self.assertEqual(failures.first().metadata["ip_address"], "127.0.0.1")
        self.assertEqual(AuditLog.objects.filter(action="login_locked_out").count(), 1)

    def test_successful_login_resets_the_account_counter(self):
        self._login("wrong-password")
        self._login("wrong-password")
        self.assertEqual(self._login(PASSWORD).status_code, 302)
        self.client.post(reverse("logout"))

        self._login("wrong-password")
        response = self._login("wrong-password")

        self.assertNotContains(response, "Too many failed sign-in attempts")

    def test_spoofed_forwarded_header_does_not_dodge_the_lockout(self):
        for index in range(3):
            self._login("wrong-password", HTTP_X_FORWARDED_FOR=f"10.0.0.{index}")

        response = self._login(PASSWORD, HTTP_X_FORWARDED_FOR="10.9.9.9")

        self.assertContains(response, "Too many failed sign-in attempts")

    def test_lockout_is_per_account_not_global(self):
        _make_admin(username="second@example.com")
        for _ in range(3):
            self._login("wrong-password")

        response = self._login(PASSWORD, username="second@example.com")

        self.assertEqual(response.status_code, 302)

    @override_settings(LOGIN_IP_MAX_ATTEMPTS=4)
    def test_address_wide_budget_stops_username_spraying(self):
        for index in range(4):
            self._login("wrong-password", username=f"guess{index}")

        response = self._login(PASSWORD)

        self.assertContains(response, "Too many failed sign-in attempts")

    def test_blank_submissions_are_not_counted(self):
        for _ in range(5):
            self._login("")

        self.assertEqual(self._login(PASSWORD).status_code, 302)
        self.assertFalse(AuditLog.objects.filter(action="login_failed").exists())


class ClientIpTests(TestCase):
    def _request(self):
        return RequestFactory().get("/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.1", REMOTE_ADDR="198.51.100.4")

    @override_settings(TRUST_PROXY=False, USE_X_FORWARDED_HOST=True)
    def test_forwarded_header_is_ignored_without_a_trusted_proxy(self):
        self.assertEqual(throttling.client_ip(self._request()), "198.51.100.4")

    @override_settings(TRUST_PROXY=True)
    def test_forwarded_header_is_used_behind_a_trusted_proxy(self):
        self.assertEqual(throttling.client_ip(self._request()), "203.0.113.7")


@override_settings(DEPLOYMENT_MODE="cloud", EMAIL_BACKEND=LOCMEM_EMAIL)
class AccountEnumerationTests(CacheIsolatedTestCase):
    def test_wrong_password_on_unverified_account_looks_like_any_other_failure(self):
        _pending_owner()

        pending = self.client.post(reverse("login"), {"username": "pending@example.com", "password": "wrong-password"})
        unknown = self.client.post(reverse("login"), {"username": "nobody@example.com", "password": "wrong-password"})

        self.assertNotContains(pending, "Verify your email")
        self.assertEqual(
            [str(message) for message in pending.context["messages"]],
            [str(message) for message in unknown.context["messages"]],
        )
        self.assertEqual(pending.context["form"].non_field_errors(), unknown.context["form"].non_field_errors())

    def test_right_password_on_unverified_account_offers_the_resend_path(self):
        _pending_owner()

        response = self.client.post(reverse("login"), {"username": "pending@example.com", "password": PASSWORD})

        self.assertContains(response, "Verify your email before signing in")
        self.assertContains(response, "Send the verification email again")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_disabled_account_gets_the_generic_error(self):
        _make_admin(username="active@example.com")
        get_user_model().objects.create_user(username="gone", password=PASSWORD, is_active=False)

        response = self.client.post(reverse("login"), {"username": "gone", "password": PASSWORD})

        self.assertNotContains(response, "inactive")
        self.assertNotContains(response, "Verify your email")
        self.assertNotIn("_auth_user_id", self.client.session)

    def _signup(self, email):
        return self.client.post(
            reverse("signup"),
            {
                "first_name": "Owner",
                "last_name": "User",
                "email": email,
                "password1": PASSWORD,
                "password2": PASSWORD,
                "selected_plan": OwnerOnboarding.PLAN_STARTER,
            },
        )

    def test_signup_with_a_registered_email_answers_like_a_new_one(self):
        get_user_model().objects.create_user(username="taken@example.com", email="taken@example.com", is_active=False)

        response = self._signup("taken@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Check your email")
        self.assertNotContains(response, "already exists")
        self.assertEqual(get_user_model().objects.filter(email__iexact="taken@example.com").count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_signup_again_replaces_the_stale_unverified_signup(self):
        self._signup("owner@example.com")
        response = self._signup("owner@example.com")

        self.assertContains(response, "Check your email")
        self.assertEqual(get_user_model().objects.filter(email="owner@example.com").count(), 1)
        self.assertEqual(len(mail.outbox), 2)

    def test_signup_survives_a_mail_outage_and_offers_a_retry(self):
        with mock.patch("pos.views.public.send_mail", side_effect=smtplib.SMTPException("smtp down")):
            response = self._signup("owner@example.com")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be sent just now")
        self.assertContains(response, "Resend verification")
        user = get_user_model().objects.get(email="owner@example.com")
        self.assertFalse(user.is_active)
        self.assertIsNone(user.owner_onboarding.verification_sent_at)

        retry = self.client.post(reverse("resend_verification_email"), {"email": "owner@example.com"})

        self.assertEqual(retry.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        user.owner_onboarding.refresh_from_db()
        self.assertIsNotNone(user.owner_onboarding.verification_sent_at)

    def test_resend_gives_the_same_answer_for_known_and_unknown_addresses(self):
        _pending_owner()

        known = self.client.post(reverse("resend_verification_email"), {"email": "pending@example.com"})
        unknown = self.client.post(reverse("resend_verification_email"), {"email": "nobody@example.com"})

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(
            [str(message) for message in known.context["messages"]],
            [str(message) for message in unknown.context["messages"]],
        )
        def normalised(response, email):
            page = response.content.decode().replace(email, "X")
            return re.sub(r'name="csrfmiddlewaretoken" value="[^"]+"', "", page)

        self.assertEqual(normalised(known, "pending@example.com"), normalised(unknown, "nobody@example.com"))
        self.assertEqual([message.to for message in mail.outbox], [["pending@example.com"]])

    def test_resend_is_rate_limited_per_email(self):
        _pending_owner()

        for _ in range(3):
            self.client.post(reverse("resend_verification_email"), {"email": "pending@example.com"})
        response = self.client.post(reverse("resend_verification_email"), {"email": "PENDING@example.com"})

        self.assertEqual(len(mail.outbox), 3)
        self.assertContains(response, "Too many verification emails")

    def test_resend_is_rate_limited_per_address(self):
        with mock.patch("pos.views.public.RESEND_IP_LIMIT", 2):
            for index in range(2):
                self.client.post(reverse("resend_verification_email"), {"email": f"user{index}@example.com"})
            response = self.client.post(reverse("resend_verification_email"), {"email": "another@example.com"})

        self.assertContains(response, "Too many verification emails")

    def test_leftover_verification_link_cannot_create_a_second_owner(self):
        user, _onboarding = _pending_owner()
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        _make_admin(username="real-owner@example.com")

        response = self.client.get(reverse("verify_signup_email", args=[uid, token]), follow=True)

        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertContains(response, "no longer valid")


@override_settings(DEPLOYMENT_MODE="cloud", EMAIL_BACKEND=LOCMEM_EMAIL)
class PasswordResetTests(CacheIsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.user = _make_admin()

    def _reset_link_from_mail(self):
        body = mail.outbox[-1].body
        return next(line for line in body.splitlines() if "/password/reset/" in line).strip()

    def test_reset_pages_are_public_and_change_page_is_not(self):
        self.assertEqual(self.client.get(reverse("password_reset")).status_code, 200)
        self.assertEqual(self.client.get(reverse("password_reset_done")).status_code, 200)

        response = self.client.get(reverse("password_change"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_full_reset_flow_signs_the_user_in(self):
        response = self.client.post(reverse("password_reset"), {"email": "owner@example.com"})

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)

        form_page = self.client.get(self._reset_link_from_mail(), follow=True)
        self.assertContains(form_page, "Choose a new password")
        set_password_url = form_page.redirect_chain[-1][0]

        done = self.client.post(
            set_password_url,
            {"new_password1": "EvenStronger456!", "new_password2": "EvenStronger456!"},
        )

        self.assertEqual(done.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("EvenStronger456!"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertTrue(AuditLog.objects.filter(action="password_reset_completed", actor=self.user).exists())

    def test_unknown_email_gets_the_same_response_and_no_mail(self):
        response = self.client.post(reverse("password_reset"), {"email": "nobody@example.com"})

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_unverified_signup_cannot_use_password_reset(self):
        _pending_owner()

        self.client.post(reverse("password_reset"), {"email": "pending@example.com"})

        self.assertEqual(len(mail.outbox), 0)

    def test_reset_requests_are_rate_limited_per_email(self):
        for _ in range(5):
            response = self.client.post(reverse("password_reset"), {"email": "owner@example.com"})

        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 3)

    def test_verification_token_is_not_a_password_reset_token(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        verification_token = default_token_generator.make_token(self.user)

        self.assertFalse(password_reset_token_generator.check_token(self.user, verification_token))
        response = self.client.get(reverse("password_reset_confirm", args=[uid, verification_token]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This link has expired")

    def test_password_reset_token_cannot_verify_an_email(self):
        pending_user, onboarding = _pending_owner()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        uid = urlsafe_base64_encode(force_bytes(pending_user.pk))
        reset_token = password_reset_token_generator.make_token(pending_user)

        self.client.get(reverse("verify_signup_email", args=[uid, reset_token]))

        onboarding.refresh_from_db()
        pending_user.refresh_from_db()
        self.assertFalse(onboarding.email_verified)
        self.assertFalse(pending_user.is_active)

    def test_change_password_needs_the_current_password_and_is_audited(self):
        self.client.force_login(self.user)

        wrong = self.client.post(
            reverse("password_change"),
            {"old_password": "not-it", "new_password1": "EvenStronger456!", "new_password2": "EvenStronger456!"},
        )
        self.assertEqual(wrong.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

        right = self.client.post(
            reverse("password_change"),
            {"old_password": PASSWORD, "new_password1": "EvenStronger456!", "new_password2": "EvenStronger456!"},
        )

        self.assertEqual(right.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("EvenStronger456!"))
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.pk))
        self.assertTrue(AuditLog.objects.filter(action="password_changed", actor=self.user).exists())

    def test_change_password_page_renders_inside_the_app(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("password_change"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current password")

    @override_settings(DEPLOYMENT_MODE="desktop")
    def test_desktop_mode_explains_offline_recovery_instead_of_emailing(self):
        page = self.client.get(reverse("password_reset"))
        self.assertContains(page, "reset_owner_password")

        self.client.post(reverse("password_reset"), {"email": "owner@example.com"})

        self.assertEqual(len(mail.outbox), 0)


@override_settings(DEPLOYMENT_MODE="desktop", EMAIL_BACKEND=LOCMEM_EMAIL)
class DesktopModeTests(CacheIsolatedTestCase):
    def test_home_skips_the_landing_page(self):
        self.assertRedirects(self.client.get(reverse("home")), reverse("signup"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("login")), reverse("signup"), fetch_redirect_response=False)

        _make_admin()

        self.assertRedirects(self.client.get(reverse("home")), reverse("login"), fetch_redirect_response=False)

    def test_signup_verifies_signs_in_and_continues_to_business_setup(self):
        response = self.client.post(
            reverse("signup"),
            {
                "first_name": "Shop",
                "last_name": "Owner",
                "email": "shop@example.com",
                "password1": PASSWORD,
                "password2": PASSWORD,
                "selected_plan": OwnerOnboarding.PLAN_STARTER,
            },
        )

        self.assertRedirects(response, reverse("setup_admin"), fetch_redirect_response=False)
        user = get_user_model().objects.get(email="shop@example.com")
        self.assertTrue(user.is_active)
        self.assertTrue(user.owner_onboarding.email_verified)
        self.assertEqual(self.client.session["_auth_user_id"], str(user.pk))
        self.assertEqual(len(mail.outbox), 0)

        setup_page = self.client.get(reverse("setup_admin"))
        self.assertEqual(setup_page.status_code, 200)
        self.assertNotContains(setup_page, "3 free days")

    def test_signup_and_login_pages_hide_google_and_marketing(self):
        signup_page = self.client.get(reverse("signup"))
        self.assertNotContains(signup_page, "Google")
        self.assertNotContains(signup_page, "3 free days")
        self.assertNotContains(signup_page, "Back to website")

        _make_admin()
        login_page = self.client.get(reverse("login"))
        self.assertNotContains(login_page, "Google")
        self.assertNotContains(login_page, "Back to website")
        self.assertContains(login_page, "Forgot your password?")

    @override_settings(GOOGLE_SIGN_IN_ENABLED=True, GOOGLE_OAUTH_CLIENT_ID="id", GOOGLE_OAUTH_CLIENT_SECRET="secret")
    def test_google_routes_are_closed(self):
        _make_admin()

        response = self.client.get(reverse("google_auth_start"))

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("accounts.google.com", response.url)


class GoogleSubjectStorageTests(TestCase):
    def test_lookup_degrades_to_email_matching_until_the_column_exists(self):
        if google_sub_storage_available():
            self.skipTest("UserPreference.google_sub exists; covered by the Google sign-in tests.")
        self.assertIsNone(find_user_by_google_sub("google-user-1"))


class TeamUserFormTests(AuthenticatedAdminTestCase):
    def _form(self, **overrides):
        data = {
            "username": "cashier1",
            "email": "",
            "role": CASHIER_ROLE,
            "is_active": "on",
            "password1": PASSWORD,
            "password2": PASSWORD,
        }
        data.update(overrides)
        return TeamUserForm(data)

    def test_login_id_cannot_shadow_another_accounts_email(self):
        get_user_model().objects.create_user(username="boss", email="boss@example.com", password=PASSWORD)

        self.assertFalse(self._form(username="BOSS@example.com").is_valid())
        self.assertFalse(self._form(username="Boss").is_valid())
        self.assertFalse(self._form(email="Boss@Example.com").is_valid())
        self.assertTrue(self._form().is_valid())


class ApiSecurityTests(AuthenticatedAdminTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for title, acc_type in [
            ("Cash", Account.ASSET),
            ("Bank", Account.ASSET),
            ("Accounts Receivable", Account.ASSET),
            ("Accounts Payable", Account.LIABILITY),
        ]:
            Account.objects.create(title=title, type=acc_type)
        cls.customer = Customer.objects.create(name="API Customer")

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def _post_settlement(self, **payload):
        body = {"customer_id": self.customer.id, "amount": "25.00", "payment_method": "cash"}
        body.update(payload)
        return self.client.post(reverse("settlement_api"), data=json.dumps(body), content_type="application/json")

    def test_basic_auth_credentials_are_not_accepted(self):
        self.client.logout()
        credentials = base64.b64encode(b"admin:AdminPass123!").decode()

        response = self.client.get(
            reverse("party_statement_api"),
            {"customer_id": self.customer.id},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )

        self.assertIn(response.status_code, {401, 403})

    def test_anonymous_requests_are_rejected(self):
        self.client.logout()

        self.assertIn(self._post_settlement().status_code, {401, 403})
        self.assertFalse(Settlement.objects.exists())

    def test_locked_workspace_blocks_reads_and_writes(self):
        with mock.patch("api.views.workspace_is_locked", return_value=True):
            read = self.client.get(reverse("party_statement_api"), {"customer_id": self.customer.id})
            write = self._post_settlement()

        self.assertEqual(read.status_code, 403)
        self.assertEqual(write.status_code, 403)
        self.assertFalse(Settlement.objects.exists())

    def test_cashier_without_party_access_is_forbidden(self):
        cashier = get_user_model().objects.create_user(username="till", password=PASSWORD)
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)

        self.assertEqual(self.client.get(reverse("party_statement_api"), {"customer_id": self.customer.id}).status_code, 403)
        self.assertEqual(self._post_settlement().status_code, 403)

    def test_settlement_write_is_audited(self):
        response = self._post_settlement()

        self.assertEqual(response.status_code, 201)
        audit = AuditLog.objects.get(action="settlement_receipt_created")
        self.assertEqual(audit.actor, self.admin_user)
        self.assertEqual(audit.target_id, str(response.json()["voucher_id"]))
        self.assertEqual(audit.metadata["amount"], "25.00")
        self.assertEqual(audit.metadata["source"], "api")

    def test_bank_ledger_on_a_cash_settlement_is_rejected(self):
        bank_ledger = BankAccount.objects.create(name="Main Bank", account_number="001")

        response = self._post_settlement(bank_account_id=bank_ledger.id)

        self.assertEqual(response.status_code, 400)
        self.assertIn("bank_account_id", response.json())
        self.assertFalse(Settlement.objects.exists())

    def test_bank_settlement_uses_the_chosen_ledger(self):
        bank_ledger = BankAccount.objects.create(name="Main Bank", account_number="001")

        response = self._post_settlement(payment_method="bank", bank_account_id=bank_ledger.id)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Settlement.objects.get().bank_account, bank_ledger)

    def test_paging_parameters_are_validated(self):
        url = reverse("party_statement_api")
        for bad in ({"limit": "0"}, {"limit": "abc"}, {"limit": "100000"}, {"limit": "5", "offset": "-1"}, {"offset": "5"}):
            response = self.client.get(url, {"customer_id": self.customer.id, **bad})
            self.assertEqual(response.status_code, 400, bad)

        self.assertEqual(self.client.get(url, {"customer_id": "abc"}).status_code, 400)

    def test_paging_slices_rows_and_reports_the_total(self):
        self._post_settlement(amount="10.00")
        self._post_settlement(amount="20.00")
        self._post_settlement(amount="30.00")

        response = self.client.get(
            reverse("party_statement_api"), {"customer_id": self.customer.id, "limit": "2", "offset": "1"}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_rows"], 3)
        self.assertEqual(len(payload["rows"]), 2)
        self.assertEqual(payload["closing_balance"], "-60.00")


class ResetOwnerPasswordCommandTests(TestCase):
    def test_generates_a_temporary_password_for_the_owner(self):
        from io import StringIO

        from django.core.management import call_command

        owner = _make_admin()
        OwnerOnboarding.objects.create(user=owner, selected_plan=OwnerOnboarding.PLAN_STARTER, email_verified=True)
        output = StringIO()

        call_command("reset_owner_password", stdout=output)

        temporary_password = output.getvalue().split("Temporary password: ")[1].splitlines()[0].strip()
        owner.refresh_from_db()
        self.assertTrue(owner.check_password(temporary_password))
        self.assertTrue(AuditLog.objects.filter(action="password_reset_offline", target_id=str(owner.pk)).exists())
