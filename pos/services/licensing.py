"""Offline licence keys.

A key is ``base64url(JSON payload).base64url(Ed25519 signature)``. The app only ever holds
the PUBLIC key, so it can check a key with no internet and cannot mint one. The vendor
keeps licensing/private_key.pem and signs keys with ``manage.py license_issue``.
"""
import base64
import binascii
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

DEPLOYMENT_DESKTOP = "desktop"
DEPLOYMENT_CLOUD = "cloud"
LICENSE_DIR_NAME = "licensing"
PRIVATE_KEY_FILENAME = "private_key.pem"
PUBLIC_KEY_FILENAME = "public_key.pem"
# A key "issued in the future" means this computer's clock is behind; allow for time zones and drift.
ISSUED_AT_TOLERANCE = timedelta(days=1)
MAX_KEY_LENGTH = 4096


class LicenseError(ValueError):
    """Raised with a message that is safe to show to the shop owner."""


@dataclass(frozen=True)
class LicensePayload:
    licensee: str
    email: str
    plan: str
    issued_at: datetime
    expires_at: datetime | None
    max_users: int | None
    license_id: str = ""
    # The feature ceiling. None means every module: Enterprise, or a key issued before editions existed.
    modules: tuple | None = None
    edition: str = ""

    @property
    def is_perpetual(self):
        return self.expires_at is None

    def is_expired(self, now=None):
        if self.expires_at is None:
            return False
        return (now or timezone.now()) > self.expires_at


def deployment_mode():
    mode = str(getattr(settings, "DEPLOYMENT_MODE", DEPLOYMENT_CLOUD) or DEPLOYMENT_CLOUD).strip().lower()
    return DEPLOYMENT_DESKTOP if mode == DEPLOYMENT_DESKTOP else DEPLOYMENT_CLOUD


def is_desktop_mode():
    return deployment_mode() == DEPLOYMENT_DESKTOP


def license_dir():
    return Path(settings.BASE_DIR) / LICENSE_DIR_NAME


# ---------------------------------------------------------------- encoding
def _b64encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text):
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def normalize_key(key):
    """Keys get pasted from emails and chat apps: drop every space and line break."""
    return "".join(str(key or "").split())


def key_fingerprint(key):
    return hashlib.sha256(normalize_key(key).encode("utf-8")).hexdigest()


def _isoformat(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value, field):
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LicenseError(f"This licence key has an unreadable {field}.") from exc
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


# -------------------------------------------------------------------- keys
def generate_keypair():
    """Return (private_pem, public_pem) bytes for a brand new Ed25519 signing key."""
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def load_private_key(path):
    private_key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise LicenseError("The private key file is not an Ed25519 key.")
    return private_key


def public_key_pem():
    """LICENSE_PUBLIC_KEY from settings, else the licensing/public_key.pem shipped with the app."""
    configured = str(getattr(settings, "LICENSE_PUBLIC_KEY", "") or "").strip()
    if configured:
        # Environment variables usually carry the PEM with escaped line breaks.
        return configured.replace("\\n", "\n")
    candidates = [
        license_dir() / PUBLIC_KEY_FILENAME,
        Path(__file__).resolve().parents[2] / LICENSE_DIR_NAME / PUBLIC_KEY_FILENAME,
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="ascii").strip()
        except OSError:
            continue
    return ""


@lru_cache(maxsize=4)
def _public_key_from_pem(pem):
    public_key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("not an Ed25519 public key")
    return public_key


def load_public_key():
    pem = public_key_pem()
    if not pem:
        return None
    try:
        return _public_key_from_pem(pem)
    except (ValueError, TypeError, UnicodeEncodeError):
        return None


# ----------------------------------------------------------------- signing
def build_payload(
    *,
    licensee,
    email="",
    plan="",
    days=None,
    max_users=None,
    issued_at=None,
    license_id="",
    edition="",
    modules=None,
):
    """Vendor side: the dict that gets signed. days=None means a perpetual licence.

    modules=None signs a key with no feature ceiling (everything). A list is checked
    against the module registry, so a typo can never be signed into a key.
    """
    from pos.services.modules import ModuleError, parse_module_list

    licensee = (licensee or "").strip()
    if not licensee:
        raise LicenseError("A licensee name is required.")
    if days is not None and int(days) <= 0:
        raise LicenseError("Licence days must be a positive number.")
    if max_users is not None and int(max_users) <= 0:
        raise LicenseError("Max users must be a positive number.")
    if modules is not None:
        try:
            modules = list(parse_module_list(modules))
        except ModuleError as exc:
            raise LicenseError(str(exc)) from exc
    issued_at = issued_at or timezone.now()
    expires_at = issued_at + timedelta(days=int(days)) if days is not None else None
    payload = {
        "license_id": license_id or uuid.uuid4().hex,
        "licensee": licensee,
        "email": (email or "").strip().lower(),
        "plan": (plan or "").strip().lower(),
        "issued_at": _isoformat(issued_at),
        "expires_at": _isoformat(expires_at),
        "max_users": int(max_users) if max_users is not None else None,
    }
    # Left out when unset, so a key with everything looks exactly like the keys issued before editions.
    edition = (edition or "").strip().lower()
    if edition:
        payload["edition"] = edition
    if modules is not None:
        payload["modules"] = modules
    return payload


def sign_license(payload, private_key):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"{_b64encode(body)}.{_b64encode(private_key.sign(body))}"


# ------------------------------------------------------------ verification
@lru_cache(maxsize=32)
def _verified_payload(key, pem):
    """Signature + structure check only (no clock involved), so the result can be cached."""
    if len(key) > MAX_KEY_LENGTH or key.count(".") != 1:
        raise LicenseError("That does not look like a licence key. Paste the whole key exactly as you received it.")
    body_part, signature_part = key.split(".")
    try:
        body = _b64decode(body_part)
        signature = _b64decode(signature_part)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise LicenseError("That does not look like a licence key. Paste the whole key exactly as you received it.") from exc

    try:
        _public_key_from_pem(pem).verify(signature, body)
    except InvalidSignature as exc:
        raise LicenseError("This licence key is not valid for this app.") from exc
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise LicenseError("Licence checking is not set up on this install. Contact support.") from exc

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseError("This licence key could not be read.") from exc
    if not isinstance(data, dict):
        raise LicenseError("This licence key could not be read.")

    licensee = str(data.get("licensee") or "").strip()
    issued_at = _parse_timestamp(data.get("issued_at"), "issue date")
    if not licensee or issued_at is None:
        raise LicenseError("This licence key is missing required details.")

    max_users = data.get("max_users")
    if max_users is not None:
        if isinstance(max_users, bool) or not isinstance(max_users, int) or max_users <= 0:
            raise LicenseError("This licence key has an unreadable user limit.")

    modules = data.get("modules")
    if modules is not None:
        if not isinstance(modules, list) or not all(isinstance(item, str) for item in modules):
            raise LicenseError("This licence key has an unreadable feature list.")
        # Names this version does not know (a key from a newer build) are simply ignored later.
        modules = tuple(item.strip().lower() for item in modules)

    return LicensePayload(
        licensee=licensee[:200],
        email=str(data.get("email") or "").strip().lower()[:254],
        plan=str(data.get("plan") or "").strip().lower()[:40],
        issued_at=issued_at,
        expires_at=_parse_timestamp(data.get("expires_at"), "expiry date"),
        max_users=max_users,
        license_id=str(data.get("license_id") or "")[:64],
        modules=modules,
        edition=str(data.get("edition") or "").strip().lower()[:40],
    )


def verify_license_key(key, *, now=None, allow_expired=False):
    """Return the signed LicensePayload, or raise LicenseError with an owner-friendly reason."""
    key = normalize_key(key)
    if not key:
        raise LicenseError("Paste the licence key first.")
    pem = public_key_pem()
    if not pem:
        raise LicenseError("Licence checking is not set up on this install. Contact support.")

    payload = _verified_payload(key, pem)
    now = now or timezone.now()
    if payload.issued_at > now + ISSUED_AT_TOLERANCE:
        raise LicenseError("This computer's date looks wrong. Set the correct date and time, then try again.")
    if not allow_expired and payload.is_expired(now):
        raise LicenseError(f"This licence key expired on {payload.expires_at:%d %b %Y}. Ask for a renewal key.")
    return payload


# ---------------------------------------------------------------- database
def _trusted_now():
    """Latest moment this install has seen, so winding the clock back cannot revive an expired key."""
    from pos.models import OwnerOnboarding

    now = timezone.now()
    last_seen = (
        OwnerOnboarding.objects.filter(last_seen_at__isnull=False)
        .order_by("-last_seen_at")
        .values_list("last_seen_at", flat=True)
        .first()
    )
    return last_seen if last_seen and last_seen > now else now


def get_active_license(now=None):
    """The activated licence that currently unlocks the workspace, or None."""
    from pos.models import License

    now = now or _trusted_now()
    best = None
    for candidate in License.objects.filter(is_active=True):
        try:
            payload = verify_license_key(candidate.key, now=now)
        except LicenseError:
            continue
        # Trust the signed payload, not the editable columns.
        candidate.expires_at = payload.expires_at
        candidate.max_users = payload.max_users
        candidate.modules = None if payload.modules is None else list(payload.modules)
        candidate.edition = payload.edition
        if best is None or _outlasts(candidate, best):
            best = candidate
    return best


def _outlasts(candidate, other):
    if other.expires_at is None:
        return False
    return candidate.expires_at is None or candidate.expires_at > other.expires_at


def has_valid_license(now=None):
    return get_active_license(now=now) is not None


@transaction.atomic
def activate_license(key, user=None):
    """Verify and store a pasted key. Activating the same key twice is harmless."""
    from pos.models import License

    key = normalize_key(key)
    payload = verify_license_key(key, now=_trusted_now())
    actor = user if getattr(user, "is_authenticated", False) else None
    license_obj, created = License.objects.get_or_create(
        key_hash=key_fingerprint(key),
        defaults={
            "key": key,
            "license_id": payload.license_id,
            "licensee": payload.licensee,
            "email": payload.email,
            "plan": payload.plan,
            "issued_at": payload.issued_at,
            "expires_at": payload.expires_at,
            "max_users": payload.max_users,
            "edition": payload.edition,
            "modules": None if payload.modules is None else list(payload.modules),
            "activated_by": actor,
        },
    )
    if not created and not license_obj.is_active:
        license_obj.is_active = True
        license_obj.activated_at = timezone.now()
        license_obj.activated_by = actor
        license_obj.save(update_fields=["is_active", "activated_at", "activated_by"])
    # One licence is in force at a time, so the user limit is never ambiguous.
    License.objects.filter(is_active=True).exclude(pk=license_obj.pk).update(is_active=False)
    # update() sends no signals and calls no save(): the feature ceiling may just have changed.
    from pos.services.modules import invalidate_module_state

    invalidate_module_state()
    return license_obj


# -------------------------------------------------------------- user limit
def license_user_limit():
    """Max active users allowed by the licence in force; None means unlimited (also during the trial)."""
    active = get_active_license()
    return active.max_users if active is not None else None


def active_user_count():
    return get_user_model().objects.filter(is_active=True).count()


def team_user_limit_message(adding=1):
    """Empty string when another user may be added, otherwise the reason to show the admin."""
    limit = license_user_limit()
    if limit is None:
        return ""
    if active_user_count() + int(adding) <= limit:
        return ""
    return (
        f"This licence allows {limit} active user{'s' if limit != 1 else ''}. "
        "Disable a user you no longer need, or ask for a licence with more users."
    )
