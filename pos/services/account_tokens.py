from django.contrib.auth.tokens import PasswordResetTokenGenerator


class AccountPasswordResetTokenGenerator(PasswordResetTokenGenerator):
    """Password-reset links get their own salt.

    Email verification uses Django's default generator, so without a separate salt a
    verification link could be replayed as a password-reset link (and the reverse).
    """

    key_salt = "pos.services.account_tokens.AccountPasswordResetTokenGenerator"


password_reset_token_generator = AccountPasswordResetTokenGenerator()
