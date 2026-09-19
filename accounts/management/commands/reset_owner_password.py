"""Offline password recovery for an installed shop (there is no mail server to send a reset link)."""
import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounts.services.audit import record_audit


class Command(BaseCommand):
    help = "Set a new password for the workspace owner (or --username) from the computer the shop runs on."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="", help="Login ID or email. Defaults to the workspace owner.")
        parser.add_argument("--password", default="", help="New password. A temporary one is generated when omitted.")

    def _resolve_user(self, identifier):
        user_model = get_user_model()
        if identifier:
            user = (
                user_model.objects.filter(username__iexact=identifier).first()
                or user_model.objects.filter(email__iexact=identifier).order_by("id").first()
            )
            if user is None:
                raise CommandError(f"No account matches '{identifier}'.")
            return user

        user = (
            user_model.objects.filter(owner_onboarding__isnull=False, is_active=True).order_by("id").first()
            or user_model.objects.filter(is_superuser=True, is_active=True).order_by("id").first()
        )
        if user is None:
            raise CommandError("No owner account exists yet. Open the app and create one first.")
        return user

    def handle(self, *args, **options):
        user = self._resolve_user((options["username"] or "").strip())
        password = options["password"]
        generated = not password
        if generated:
            password = secrets.token_urlsafe(9)
        else:
            try:
                validate_password(password, user)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc

        user.set_password(password)
        user.save(update_fields=["password"])
        record_audit(
            action="password_reset_offline",
            description=f"Password for {user.get_username()} was reset from the server console.",
            target=user,
        )

        self.stdout.write(self.style.SUCCESS(f"Password updated for {user.get_username()}."))
        if generated:
            self.stdout.write(f"Temporary password: {password}")
            self.stdout.write("Sign in with it, then open Change Password to choose your own.")
