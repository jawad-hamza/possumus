"""Trial length, clock-rollback guard, offline licence keys, the single access gate,
billing webhook correctness and invoice numbering."""
import hashlib
import hmac
import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pos.models import (
    BillingSubscription,
    BillingWebhookEvent,
    CompanyProfile,
    License,
    OwnerOnboarding,
    Sale,
    SystemSettings,
)
from pos.services import licensing
from pos.services.access import (
    ADMIN_ROLE,
    CASHIER_ROLE,
    assign_role,
    ensure_role_groups,
    workspace_is_locked,
)
from pos.services.billing import verify_lemon_squeezy_signature

# A throwaway key pair for the whole module. The real licensing/private_key.pem is never read here.
TEST_PRIVATE_PEM, TEST_PUBLIC_PEM = licensing.generate_keypair()
TEST_PRIVATE_KEY = serialization.load_pem_private_key(TEST_PRIVATE_PEM, password=None)
OTHER_PRIVATE_PEM, _OTHER_PUBLIC_PEM = licensing.generate_keypair()
OTHER_PRIVATE_KEY = serialization.load_pem_private_key(OTHER_PRIVATE_PEM, password=None)
WEBHOOK_SECRET = "test-lemon-secret"


def issue_key(private_key=TEST_PRIVATE_KEY, **overrides):
    options = {"licensee": "Corner Shop", "email": "owner@example.com", "plan": "standard", "days": 365}
    options.update(overrides)
    return licensing.sign_license(licensing.build_payload(**options), private_key)


def make_owner(*, trial_ended=True, username="owner@example.com"):
    ensure_role_groups()
    user = get_user_model().objects.create_user(
        username=username, email=username, password="OwnerPass123!", is_active=True, is_staff=True
    )
    assign_role(user, ADMIN_ROLE)
    now = timezone.now()
    started = now - timedelta(days=30) if trial_ended else now
    onboarding = OwnerOnboarding.objects.create(
        user=user,
        email_verified=True,
        email_verified_at=started,
        business_created_at=started,
        trial_started_at=started,
        trial_ends_at=started + timedelta(days=10),
        billing_state=OwnerOnboarding.BILLING_PENDING if trial_ended else OwnerOnboarding.BILLING_TRIAL,
    )
    if not CompanyProfile.objects.filter(is_active=True).exists():
        CompanyProfile.objects.create(name="Corner Shop", is_active=True)
    return user, onboarding


class TrialTests(TestCase):
    def test_trial_is_ten_days_by_default(self):
        _user, onboarding = make_owner(trial_ended=False)
        onboarding.trial_started_at = None
        onboarding.trial_ends_at = None
        onboarding.save()
        now = timezone.now()

        onboarding.start_trial(now=now)

        self.assertEqual(onboarding.trial_ends_at - onboarding.trial_started_at, timedelta(days=10))
        self.assertEqual(onboarding.last_seen_at, now)
        self.assertTrue(onboarding.trial_is_active)
        self.assertIn("Free trial active", onboarding.billing_status_label)

    @override_settings(TRIAL_DAYS=14)
    def test_trial_length_follows_settings(self):
        _user, onboarding = make_owner(trial_ended=False)
        onboarding.trial_started_at = None
        onboarding.trial_ends_at = None
        onboarding.save()

        onboarding.start_trial()

        self.assertEqual(onboarding.trial_ends_at - onboarding.trial_started_at, timedelta(days=14))

    def test_start_trial_never_extends_an_existing_trial(self):
        _user, onboarding = make_owner(trial_ended=True)
        ends_at = onboarding.trial_ends_at

        onboarding.start_trial()

        self.assertEqual(onboarding.trial_ends_at, ends_at)
        self.assertFalse(onboarding.trial_is_active)


class ClockRollbackTests(TestCase):
    def test_clock_moved_back_more_than_a_day_expires_the_trial(self):
        user, onboarding = make_owner(trial_ended=False)
        self.assertFalse(workspace_is_locked(user))

        # The install was last used "three days from now": the clock has been wound back.
        OwnerOnboarding.objects.filter(pk=onboarding.pk).update(last_seen_at=timezone.now() + timedelta(days=3))
        user = get_user_model().objects.get(pk=user.pk)

        self.assertTrue(user.owner_onboarding.clock_rolled_back)
        self.assertFalse(user.owner_onboarding.trial_is_active)
        self.assertTrue(workspace_is_locked(user))

    def test_small_rollback_does_not_lock_but_does_not_buy_time_either(self):
        _user, onboarding = make_owner(trial_ended=False)
        high_water = timezone.now() + timedelta(hours=6)
        onboarding.last_seen_at = high_water

        self.assertFalse(onboarding.clock_rolled_back)
        self.assertTrue(onboarding.trial_is_active)
        self.assertEqual(onboarding.trusted_now, high_water)

        # Once the high-water mark passes the end of the trial, an honest-looking clock does not help.
        onboarding.last_seen_at = onboarding.trial_ends_at + timedelta(hours=1)
        onboarding.trial_ends_at = onboarding.trial_ends_at  # unchanged
        with mock.patch("pos.models.timezone.now", return_value=onboarding.trial_ends_at - timedelta(hours=2)):
            self.assertFalse(onboarding.clock_rolled_back)
            self.assertFalse(onboarding.trial_is_active)

    def test_last_seen_only_moves_forward_and_is_throttled(self):
        _user, onboarding = make_owner(trial_ended=False)
        now = timezone.now()

        onboarding.touch_last_seen(now=now)
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.last_seen_at, now)

        onboarding.touch_last_seen(now=now + timedelta(minutes=1))
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.last_seen_at, now, "writes are throttled")

        onboarding.touch_last_seen(now=now - timedelta(days=5))
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.last_seen_at, now, "never moves backwards")

        later = now + timedelta(hours=1)
        onboarding.touch_last_seen(now=later)
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.last_seen_at, later)

    def test_gate_records_last_seen(self):
        user, onboarding = make_owner(trial_ended=False)
        self.assertIsNone(onboarding.last_seen_at)

        workspace_is_locked(user)

        onboarding.refresh_from_db()
        self.assertIsNotNone(onboarding.last_seen_at)


@override_settings(LICENSE_PUBLIC_KEY=TEST_PUBLIC_PEM.decode("ascii"))
class LicenseKeyTests(TestCase):
    def test_valid_key_round_trips(self):
        payload = licensing.verify_license_key(issue_key(max_users=3))

        self.assertEqual(payload.licensee, "Corner Shop")
        self.assertEqual(payload.email, "owner@example.com")
        self.assertEqual(payload.plan, "standard")
        self.assertEqual(payload.max_users, 3)
        self.assertFalse(payload.is_perpetual)
        self.assertEqual(payload.expires_at - payload.issued_at, timedelta(days=365))

    def test_key_format_is_payload_dot_signature(self):
        key = issue_key(days=None)
        body, signature = key.split(".")
        data = json.loads(licensing._b64decode(body))

        self.assertEqual(
            set(data), {"license_id", "licensee", "email", "plan", "issued_at", "expires_at", "max_users"}
        )
        self.assertIsNone(data["expires_at"])
        self.assertIsNone(data["max_users"])
        self.assertEqual(len(licensing._b64decode(signature)), 64)

    def test_perpetual_key_never_expires(self):
        payload = licensing.verify_license_key(issue_key(days=None), now=timezone.now() + timedelta(days=36500))
        self.assertTrue(payload.is_perpetual)

    def test_non_ascii_licensee_survives(self):
        payload = licensing.verify_license_key(issue_key(licensee="Café Zürich مارکیٹ"))
        self.assertEqual(payload.licensee, "Café Zürich مارکیٹ")

    def test_pasted_whitespace_is_ignored(self):
        key = issue_key()
        mangled = "  " + key[:40] + "\r\n" + key[40:90] + " \n" + key[90:] + "\n"
        self.assertEqual(licensing.verify_license_key(mangled).licensee, "Corner Shop")

    def test_key_signed_by_someone_else_is_rejected(self):
        with self.assertRaisesMessage(licensing.LicenseError, "not valid"):
            licensing.verify_license_key(issue_key(private_key=OTHER_PRIVATE_KEY))

    def test_edited_payload_is_rejected(self):
        body, signature = issue_key(max_users=1).split(".")
        data = json.loads(licensing._b64decode(body))
        data["max_users"] = 500
        data["expires_at"] = None
        forged_body = licensing._b64encode(json.dumps(data, sort_keys=True, separators=(",", ":")).encode())

        with self.assertRaisesMessage(licensing.LicenseError, "not valid"):
            licensing.verify_license_key(f"{forged_body}.{signature}")

    def test_garbage_is_rejected_with_a_friendly_message(self):
        for junk in ("", "   ", "hello", "a.b.c", "@@@.@@@", "x" * 5000, "abc.def"):
            with self.subTest(junk=junk[:12]):
                with self.assertRaises(licensing.LicenseError):
                    licensing.verify_license_key(junk)

    def test_expired_key_is_rejected(self):
        key = issue_key(days=30, issued_at=timezone.now() - timedelta(days=31))
        with self.assertRaisesMessage(licensing.LicenseError, "expired"):
            licensing.verify_license_key(key)
        self.assertTrue(licensing.verify_license_key(key, allow_expired=True).is_expired())

    def test_key_issued_in_the_future_means_the_clock_is_wrong(self):
        key = issue_key(issued_at=timezone.now() + timedelta(days=30))
        with self.assertRaisesMessage(licensing.LicenseError, "date looks wrong"):
            licensing.verify_license_key(key)

    @override_settings(LICENSE_PUBLIC_KEY="not a pem")
    def test_broken_public_key_never_unlocks(self):
        with self.assertRaises(licensing.LicenseError):
            licensing.verify_license_key(issue_key())

    def test_activation_stores_parsed_fields_and_is_idempotent(self):
        user, _onboarding = make_owner()
        key = issue_key(max_users=4)

        first = licensing.activate_license(key, user=user)
        second = licensing.activate_license("\n" + key + "  ", user=user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(License.objects.count(), 1)
        self.assertEqual(first.licensee, "Corner Shop")
        self.assertEqual(first.max_users, 4)
        self.assertEqual(first.activated_by, user)
        self.assertIsNotNone(first.activated_at)
        self.assertEqual(licensing.get_active_license().pk, first.pk)

    def test_new_key_supersedes_the_old_one(self):
        old = licensing.activate_license(issue_key(days=30, max_users=1))
        new = licensing.activate_license(issue_key(days=None, max_users=5))

        old.refresh_from_db()
        self.assertFalse(old.is_active)
        self.assertEqual(licensing.get_active_license().pk, new.pk)
        self.assertEqual(licensing.license_user_limit(), 5)

    def test_editing_the_database_row_unlocks_nothing(self):
        expired_key = issue_key(days=30, issued_at=timezone.now() - timedelta(days=60))
        payload = licensing.verify_license_key(expired_key, allow_expired=True)
        License.objects.create(
            key=expired_key,
            key_hash=licensing.key_fingerprint(expired_key),
            licensee=payload.licensee,
            issued_at=payload.issued_at,
            expires_at=None,  # hand-edited to look perpetual
            max_users=None,
        )
        License.objects.create(
            key="forged.key",
            key_hash="f" * 64,
            licensee="Pirate",
            issued_at=timezone.now(),
            expires_at=None,
        )

        self.assertIsNone(licensing.get_active_license())

    def test_winding_the_clock_back_does_not_revive_an_expired_licence(self):
        _user, onboarding = make_owner()
        licensing.activate_license(issue_key(days=2))
        self.assertTrue(onboarding.license_is_active)

        # The install has already been used five days from now, i.e. after the key ran out.
        OwnerOnboarding.objects.filter(pk=onboarding.pk).update(last_seen_at=timezone.now() + timedelta(days=5))
        onboarding.refresh_from_db()

        self.assertFalse(onboarding.license_is_active)
        self.assertIsNone(licensing.get_active_license())

    def test_user_limit_message(self):
        make_owner()
        self.assertEqual(licensing.team_user_limit_message(), "", "no limit during the trial")

        licensing.activate_license(issue_key(max_users=2))
        self.assertEqual(licensing.team_user_limit_message(), "")

        get_user_model().objects.create_user(username="cashier1", password="x", is_active=True)
        self.assertIn("allows 2 active users", licensing.team_user_limit_message())

        licensing.activate_license(issue_key(max_users=None, days=None))
        self.assertEqual(licensing.team_user_limit_message(), "")


class ShippedPublicKeyTests(TestCase):
    def test_app_ships_a_real_ed25519_public_key(self):
        path = licensing.license_dir() / licensing.PUBLIC_KEY_FILENAME
        self.assertTrue(path.is_file(), "run manage.py license_keygen once")
        self.assertIsInstance(serialization.load_pem_public_key(path.read_bytes()), Ed25519PublicKey)

    def test_private_key_can_never_be_committed(self):
        gitignore = licensing.license_dir() / ".gitignore"
        self.assertIn(licensing.PRIVATE_KEY_FILENAME, gitignore.read_text(encoding="utf-8").split())

    def test_keys_from_a_throwaway_pair_do_not_pass_the_shipped_key(self):
        with self.assertRaises(licensing.LicenseError):
            licensing.verify_license_key(issue_key())


class LicenseCommandTests(TestCase):
    def test_keygen_then_issue_produces_a_verifiable_key(self):
        with tempfile.TemporaryDirectory() as folder:
            call_command("license_keygen", dir=folder, stdout=StringIO())
            private_path = Path(folder) / "private_key.pem"
            public_path = Path(folder) / "public_key.pem"
            self.assertTrue(private_path.is_file())
            self.assertTrue(public_path.is_file())
            self.assertIn("private_key.pem", (Path(folder) / ".gitignore").read_text(encoding="utf-8"))

            with self.assertRaises(CommandError):
                call_command("license_keygen", dir=folder, stdout=StringIO())
            self.assertEqual(private_path.read_bytes(), private_path.read_bytes())

            out = StringIO()
            call_command(
                "license_issue",
                licensee="Test Shop",
                email="Shop@Example.com",
                plan="Desktop",
                days=90,
                max_users=2,
                private_key=str(private_path),
                stdout=out,
                stderr=StringIO(),
            )
            key = out.getvalue().strip()

            with override_settings(LICENSE_PUBLIC_KEY=public_path.read_text(encoding="ascii")):
                payload = licensing.verify_license_key(key)
        self.assertEqual(payload.licensee, "Test Shop")
        self.assertEqual(payload.email, "shop@example.com")
        self.assertEqual(payload.plan, "desktop")
        self.assertEqual(payload.max_users, 2)
        self.assertEqual(payload.expires_at - payload.issued_at, timedelta(days=90))

    def test_issue_perpetual_and_missing_private_key(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(CommandError):
                call_command(
                    "license_issue",
                    licensee="X",
                    perpetual=True,
                    private_key=str(Path(folder) / "nope.pem"),
                    stdout=StringIO(),
                    stderr=StringIO(),
                )
            call_command("license_keygen", dir=folder, stdout=StringIO())
            out = StringIO()
            call_command(
                "license_issue",
                licensee="Forever Shop",
                perpetual=True,
                private_key=str(Path(folder) / "private_key.pem"),
                stdout=out,
                stderr=StringIO(),
            )
            with override_settings(LICENSE_PUBLIC_KEY=(Path(folder) / "public_key.pem").read_text(encoding="ascii")):
                payload = licensing.verify_license_key(out.getvalue())
        self.assertTrue(payload.is_perpetual)
        self.assertIsNone(payload.max_users)


@override_settings(LICENSE_PUBLIC_KEY=TEST_PUBLIC_PEM.decode("ascii"))
class AccessGateTests(TestCase):
    def test_expired_trial_locks_every_protected_page_including_invoices(self):
        user, _onboarding = make_owner()
        sale = Sale.objects.create(sold_by=user)
        self.client.force_login(user)

        for url in (
            reverse("dashboard"),
            reverse("pos_screen"),
            reverse("product_list"),
            reverse("invoice", args=[sale.id]),
            reverse("invoice_pdf", args=[sale.id]),
            reverse("accounts_home"),
            reverse("settings_page"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.url, reverse("billing_page"))

    def test_locked_json_request_gets_402_not_an_html_redirect(self):
        user, _onboarding = make_owner()
        self.client.force_login(user)

        response = self.client.get(reverse("pos_screen"), HTTP_ACCEPT="application/json")

        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["redirect"], reverse("billing_page"))

    def test_licence_and_billing_pages_stay_reachable_when_locked(self):
        user, _onboarding = make_owner()
        self.client.force_login(user)

        licence_response = self.client.get(reverse("license_page"))
        billing_response = self.client.get(reverse("billing_page"))

        self.assertEqual(licence_response.status_code, 200)
        self.assertContains(licence_response, 'name="license_key"')
        self.assertEqual(billing_response.status_code, 200)
        self.assertContains(billing_response, 'name="license_key"')
        self.assertContains(billing_response, "The free trial has ended")
        # The plan feature bullets come from pos/views/_shared.py; only the page's own copy is checked here.
        self.assertNotContains(billing_response, "free days have ended")
        self.assertNotContains(billing_response, "your 3 free days")

    def test_pasting_a_valid_key_unlocks_and_goes_straight_back_to_work(self):
        user, onboarding = make_owner()
        self.client.force_login(user)
        self.assertTrue(workspace_is_locked(user))

        response = self.client.post(reverse("license_page"), {"license_key": issue_key(days=None)})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("dashboard"))
        self.assertFalse(workspace_is_locked(get_user_model().objects.get(pk=user.pk)))
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.billing_status_label, "Licensed (lifetime)")
        self.assertEqual(License.objects.get().activated_by, user)

    def test_bad_key_keeps_the_workspace_locked_and_explains_why(self):
        user, _onboarding = make_owner()
        self.client.force_login(user)

        response = self.client.post(reverse("license_page"), {"license_key": issue_key(private_key=OTHER_PRIVATE_KEY)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not valid for this app")
        self.assertFalse(License.objects.exists())
        self.assertTrue(workspace_is_locked(user))

    def test_licence_unlocks_the_whole_team_but_only_admins_can_activate(self):
        _owner, _onboarding = make_owner()
        cashier = get_user_model().objects.create_user(username="till", password="CashierPass123!", is_active=True)
        assign_role(cashier, CASHIER_ROLE)
        self.client.force_login(cashier)

        self.assertEqual(self.client.get(reverse("pos_screen")).url, reverse("billing_page"))
        self.client.post(reverse("license_page"), {"license_key": issue_key()})
        self.assertFalse(License.objects.exists())

        licensing.activate_license(issue_key())
        self.assertEqual(self.client.get(reverse("pos_screen")).status_code, 200)

    def test_anonymous_visitor_is_sent_to_login(self):
        make_owner()
        response = self.client.get(reverse("license_page"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("login")))

    def test_active_subscription_still_unlocks(self):
        user, onboarding = make_owner()
        BillingSubscription.objects.create(owner_onboarding=onboarding, status=BillingSubscription.STATUS_ACTIVE)
        self.assertFalse(workspace_is_locked(get_user_model().objects.get(pk=user.pk)))

    def test_workspace_without_an_owner_record_is_not_locked(self):
        ensure_role_groups()
        admin = get_user_model().objects.create_user(username="legacy", password="x", is_staff=True)
        self.assertFalse(workspace_is_locked(admin))

    @override_settings(DEPLOYMENT_MODE="desktop")
    def test_desktop_mode_shows_trial_and_licence_only(self):
        user, onboarding = make_owner(trial_ended=False)
        self.client.force_login(user)

        response = self.client.get(reverse("billing_page"))

        self.assertContains(response, 'name="license_key"')
        self.assertContains(response, "10-day free trial")
        self.assertNotContains(response, 'id="plans"')
        self.assertNotContains(response, "Lemon Squeezy")

        self.client.post(reverse("billing_checkout", args=[OwnerOnboarding.PLAN_GROWTH]))
        self.assertFalse(BillingSubscription.objects.filter(owner_onboarding=onboarding).exists())

    def test_cloud_mode_keeps_plans_and_offers_the_licence_key_too(self):
        user, _onboarding = make_owner(trial_ended=False)
        self.client.force_login(user)

        response = self.client.get(reverse("billing_page"))

        self.assertContains(response, 'id="plans"')
        self.assertContains(response, 'name="license_key"')


class BillingCorrectnessTests(TestCase):
    def _payload(self, onboarding, *, status="active", updated_at="2026-05-01T10:00:00Z", event="subscription_updated", **attributes):
        body = {
            "meta": {
                "event_name": event,
                "custom_data": {"owner_onboarding_id": str(onboarding.id), "plan_code": OwnerOnboarding.PLAN_GROWTH},
            },
            "data": {
                "type": "subscriptions",
                "id": attributes.pop("subscription_id", "sub_1"),
                "attributes": {
                    "status": status,
                    "updated_at": updated_at,
                    "variant_id": 111,
                    "product_id": 999,
                    **attributes,
                },
            },
        }
        return body

    def _post(self, payload, signature=None):
        raw = json.dumps(payload).encode("utf-8")
        if signature is None:
            signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse("lemonsqueezy_webhook"), data=raw, content_type="application/json", HTTP_X_SIGNATURE=signature
        )

    def test_checkout_get_changes_nothing(self):
        user, onboarding = make_owner()
        self.client.force_login(user)

        response = self.client.get(reverse("billing_checkout", args=[OwnerOnboarding.PLAN_SCALE]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("billing_page")))
        self.assertFalse(BillingSubscription.objects.exists())
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.selected_plan, OwnerOnboarding.PLAN_STARTER)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_non_ascii_signature_is_a_clean_400(self):
        _user, onboarding = make_owner()
        self.assertFalse(verify_lemon_squeezy_signature(b"{}", "sígnature-✓"))

        response = self._post(self._payload(onboarding), signature="sígnature-✓".encode("utf-8").decode("latin-1"))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_signature")

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_failed_sync_leaves_no_event_so_the_retry_is_processed(self):
        _user, onboarding = make_owner()
        payload = self._payload(onboarding)

        with mock.patch("pos.services.billing._sync_subscription_from_payload", side_effect=RuntimeError("db down")):
            with self.assertLogs("pos.views.billing", level="ERROR"):
                failed = self._post(payload)

        self.assertEqual(failed.status_code, 500)
        self.assertFalse(BillingWebhookEvent.objects.exists())

        retry = self._post(payload)

        self.assertEqual(retry.status_code, 200)
        self.assertFalse(retry.json()["duplicate"])
        self.assertEqual(BillingSubscription.objects.get().status, BillingSubscription.STATUS_ACTIVE)

        again = self._post(payload)
        self.assertTrue(again.json()["duplicate"])
        self.assertEqual(BillingWebhookEvent.objects.count(), 1)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_older_event_does_not_undo_a_newer_one(self):
        _user, onboarding = make_owner()
        self._post(self._payload(onboarding, status="active", updated_at="2026-05-02T10:00:00Z"))

        response = self._post(self._payload(onboarding, status="expired", updated_at="2026-05-01T10:00:00Z", event="subscription_expired"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["ignored"], "stale")
        self.assertEqual(BillingSubscription.objects.get().status, BillingSubscription.STATUS_ACTIVE)

        self._post(self._payload(onboarding, status="expired", updated_at="2026-05-03T10:00:00Z", event="subscription_expired"))
        self.assertEqual(BillingSubscription.objects.get().status, BillingSubscription.STATUS_EXPIRED)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_old_subscription_expiring_does_not_lock_out_its_replacement(self):
        _user, onboarding = make_owner()
        self._post(self._payload(onboarding, subscription_id="sub_new", updated_at="2026-05-02T10:00:00Z"))

        response = self._post(
            self._payload(onboarding, subscription_id="sub_old", status="expired", updated_at="2026-05-03T10:00:00Z", event="subscription_expired")
        )

        self.assertEqual(response.json()["ignored"], "other_subscription")
        subscription = BillingSubscription.objects.get()
        self.assertEqual(subscription.provider_subscription_id, "sub_new")
        self.assertTrue(subscription.grants_access)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET, LEMON_SQUEEZY_VARIANT_PLAN_MAP={"111": "starter"})
    def test_purchased_variant_beats_buyer_editable_custom_data(self):
        _user, onboarding = make_owner()

        self._post(self._payload(onboarding))  # custom_data claims "growth", variant 111 is starter

        self.assertEqual(BillingSubscription.objects.get().plan_code, OwnerOnboarding.PLAN_STARTER)
        onboarding.refresh_from_db()
        self.assertEqual(onboarding.selected_plan, OwnerOnboarding.PLAN_STARTER)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET, LEMON_SQUEEZY_VARIANT_PLAN_MAP='{"999": "scale"}')
    def test_product_id_mapping_and_json_text_setting(self):
        _user, onboarding = make_owner()
        self._post(self._payload(onboarding))
        self.assertEqual(BillingSubscription.objects.get().plan_code, OwnerOnboarding.PLAN_SCALE)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET, LEMON_SQUEEZY_VARIANT_PLAN_MAP={"555": "scale"})
    def test_unmapped_variant_never_falls_back_to_custom_data(self):
        _user, onboarding = make_owner()
        self._post(self._payload(onboarding))
        self.assertEqual(BillingSubscription.objects.get().plan_code, OwnerOnboarding.PLAN_STARTER)

    def test_past_due_has_a_seven_day_grace_period(self):
        _user, onboarding = make_owner()
        now = timezone.now()
        subscription = BillingSubscription.objects.create(
            owner_onboarding=onboarding, status=BillingSubscription.STATUS_PAST_DUE, past_due_since=now - timedelta(days=6)
        )
        self.assertTrue(subscription.grants_access)

        subscription.past_due_since = now - timedelta(days=8)
        self.assertFalse(subscription.grants_access)
        self.assertEqual(subscription.status_tone, "advance")

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_repeated_past_due_events_do_not_restart_the_grace_clock(self):
        _user, onboarding = make_owner()
        self._post(self._payload(onboarding, status="past_due", updated_at="2026-05-01T10:00:00Z"))
        first = BillingSubscription.objects.get().past_due_since

        self._post(self._payload(onboarding, status="past_due", updated_at="2026-05-04T10:00:00Z"))
        self.assertEqual(BillingSubscription.objects.get().past_due_since, first)

        self._post(self._payload(onboarding, status="active", updated_at="2026-05-05T10:00:00Z"))
        self.assertIsNone(BillingSubscription.objects.get().past_due_since)

    def test_cancelled_without_an_end_date_is_over(self):
        _user, onboarding = make_owner()
        subscription = BillingSubscription.objects.create(owner_onboarding=onboarding, status=BillingSubscription.STATUS_CANCELLED)
        self.assertFalse(subscription.grants_access)

        subscription.ends_at = timezone.now() + timedelta(days=3)
        self.assertTrue(subscription.grants_access)

        subscription.ends_at = timezone.now() - timedelta(minutes=1)
        self.assertFalse(subscription.grants_access)

    @override_settings(LEMON_SQUEEZY_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_resume_clears_the_old_end_date(self):
        _user, onboarding = make_owner()
        self._post(self._payload(onboarding, status="cancelled", updated_at="2026-05-01T10:00:00Z", ends_at="2026-06-01T00:00:00Z"))
        self.assertIsNotNone(BillingSubscription.objects.get().ends_at)

        self._post(self._payload(onboarding, status="active", updated_at="2026-05-02T10:00:00Z", ends_at=None, event="subscription_resumed"))
        self.assertIsNone(BillingSubscription.objects.get().ends_at)


class InvoiceNumberTests(TestCase):
    def test_new_sales_get_prefixed_zero_padded_sequential_numbers(self):
        first = Sale.objects.create()
        second = Sale.objects.create()

        self.assertEqual(first.invoice_number, "INV-000001")
        self.assertEqual(second.invoice_number, "INV-000002")
        self.assertEqual(first.display_number, "INV-000001")
        self.assertEqual(str(second), "Sale INV-000002")

    def test_prefix_comes_from_settings(self):
        settings_row = SystemSettings.load()
        settings_row.invoice_prefix = " SHOP/ "
        settings_row.save()

        self.assertEqual(Sale.objects.create().invoice_number, "SHOP/000001")

    def test_legacy_sale_without_a_number_falls_back_to_its_id(self):
        sale = Sale.objects.create()
        other = Sale.objects.create()
        Sale.objects.filter(pk__in=[sale.pk, other.pk]).update(invoice_number=None)  # several NULLs are allowed
        sale.refresh_from_db()

        self.assertEqual(sale.display_number, f"#{sale.pk}")

    def test_saving_an_existing_sale_keeps_its_number(self):
        sale = Sale.objects.create()
        number = sale.invoice_number
        sale.paid_amount = 5
        sale.save()
        sale.update_total()
        sale.refresh_from_db()
        self.assertEqual(sale.invoice_number, number)

    def test_stale_counter_skips_numbers_already_used(self):
        Sale.objects.create()
        Sale.objects.create()
        # The settings form saves the whole row and can write an old counter back.
        SystemSettings.objects.filter(pk=1).update(next_invoice_sequence=1)

        self.assertEqual(Sale.objects.create().invoice_number, "INV-000003")

    def test_failed_sale_gives_its_number_back(self):
        Sale.objects.create()
        try:
            with transaction.atomic():
                Sale.objects.create()
                raise RuntimeError("checkout failed")
        except RuntimeError:
            pass

        self.assertEqual(Sale.objects.create().invoice_number, "INV-000002")
