import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == "test"


def _load_env_file(path):
    """Minimal .env reader (KEY=VALUE lines); real environment variables always win."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


# Tests stay hermetic: a developer's .env must not change what the suite exercises.
if not RUNNING_TESTS:
    _load_env_file(os.getenv("ZEHNMS_ENV_FILE") or BASE_DIR / ".env")


def env(name, default=None):
    return os.getenv(name, default)


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default=0):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return int(value)


def env_float(name, default=0.0):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return float(value)


def env_list(name, default=None):
    value = os.getenv(name)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def env_admins(name="DJANGO_ADMINS"):
    admins = []
    for entry in env_list(name):
        if ":" in entry:
            display_name, email = entry.split(":", 1)
            admins.append((display_name.strip(), email.strip()))
        else:
            admins.append((entry.strip(), entry.strip()))
    return admins


def env_json(name, default=None):
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must contain valid JSON.") from exc


def _ensure_writable_dir(path):
    """Create the folder and prove we can write into it (serverless and locked-down installs cannot)."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=str(path)):
            pass
    except OSError:
        return False
    return True


# "desktop" = one shop on one Windows PC (installer build); "cloud" = any hosted deployment.
DEPLOYMENT_MODE = (env("DEPLOYMENT_MODE", "cloud") or "cloud").strip().lower()
if DEPLOYMENT_MODE not in {"desktop", "cloud"}:
    raise ImproperlyConfigured("DEPLOYMENT_MODE must be 'desktop' or 'cloud'.")
IS_DESKTOP = DEPLOYMENT_MODE == "desktop"
ON_VERCEL = bool(env("VERCEL"))

# Everything the app writes (SQLite file, media, logs, backups) lives under DATA_DIR.
# Vercel's filesystem is read-only apart from the ephemeral /tmp.
DATA_DIR = Path(env("ZEHNMS_DATA_DIR") or ("/tmp/zehnms" if ON_VERCEL else BASE_DIR))


def _install_pymysql():
    try:
        import pymysql
    except ImportError as exc:
        raise ImproperlyConfigured(
            "PyMySQL is required when DB_ENGINE/mysql DATABASE_URL points to MySQL."
        ) from exc
    pymysql.install_as_MySQLdb()


def _build_sqlite_config(db_name=None):
    sqlite_name = db_name or env("SQLITE_PATH") or str(DATA_DIR / "db.sqlite3")
    options = {
        # Two tills writing at once must queue instead of failing with "database is locked".
        "timeout": env_int("SQLITE_TIMEOUT_SECONDS", 20),
        "transaction_mode": "IMMEDIATE",
    }
    if IS_DESKTOP:
        options["init_command"] = "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;"
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": sqlite_name,
        "OPTIONS": options,
    }


def _build_postgresql_config(db_name, *, user="", password="", host="", port="", options=None):
    config = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db_name,
        "USER": user,
        "PASSWORD": password,
        "HOST": host,
        "PORT": str(port or ""),
    }
    if options:
        config["OPTIONS"] = options
    return config


def _build_mysql_config(db_name, *, user="", password="", host="", port="", options=None):
    _install_pymysql()
    mysql_options = {"charset": "utf8mb4"}
    if options:
        mysql_options.update(options)
    return {
        "ENGINE": "django.db.backends.mysql",
        "NAME": db_name,
        "USER": user,
        "PASSWORD": password,
        "HOST": host,
        "PORT": str(port or ""),
        "OPTIONS": mysql_options,
    }


def _sqlite_name_from_url(parsed):
    if parsed.path in {"", "/"}:
        return ":memory:"
    if parsed.netloc and parsed.path:
        raw_path = f"//{parsed.netloc}{parsed.path}"
    else:
        raw_path = parsed.path or parsed.netloc
    raw_path = unquote(raw_path)
    if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return raw_path


def database_config():
    database_url = env("DATABASE_URL", "").strip()
    if database_url:
        parsed = urlparse(database_url)
        scheme = parsed.scheme.split("+", 1)[0].lower()
        query = parse_qs(parsed.query)

        if scheme in {"sqlite", "sqlite3"}:
            return _build_sqlite_config(_sqlite_name_from_url(parsed))

        if scheme in {"postgres", "postgresql", "pgsql"}:
            # Hosted Postgres (Neon, Supabase) puts libpq options in the URL query string.
            options = {
                key: query[key][-1]
                for key in ("sslmode", "channel_binding", "options", "connect_timeout")
                if query.get(key)
            }
            return _build_postgresql_config(
                db_name=unquote(parsed.path.lstrip("/")),
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                host=parsed.hostname or "",
                port=parsed.port or "",
                options=options,
            )

        if scheme in {"mysql"}:
            options = {}
            if query.get("charset"):
                options["charset"] = query["charset"][-1]
            return _build_mysql_config(
                db_name=unquote(parsed.path.lstrip("/")),
                user=unquote(parsed.username or ""),
                password=unquote(parsed.password or ""),
                host=parsed.hostname or "",
                port=parsed.port or "",
                options=options,
            )

        raise ImproperlyConfigured(f"Unsupported DATABASE_URL scheme '{parsed.scheme}'.")

    engine = env("DB_ENGINE", "sqlite").strip().lower()
    if engine in {"sqlite", "sqlite3"}:
        return _build_sqlite_config()
    if engine in {"postgres", "postgresql"}:
        return _build_postgresql_config(
            db_name=env("DB_NAME", "possumus"),
            user=env("DB_USER", ""),
            password=env("DB_PASSWORD", ""),
            host=env("DB_HOST", ""),
            port=env("DB_PORT", ""),
            options={"sslmode": env("DB_SSLMODE", "prefer")} if env("DB_SSLMODE") else None,
        )
    if engine == "mysql":
        return _build_mysql_config(
            db_name=env("DB_NAME", "possumus"),
            user=env("DB_USER", ""),
            password=env("DB_PASSWORD", ""),
            host=env("DB_HOST", ""),
            port=env("DB_PORT", ""),
        )
    raise ImproperlyConfigured(f"Unsupported DB_ENGINE '{engine}'.")


INSECURE_DEFAULT_SECRET_KEY = "django-insecure-%kju_qo0cwbk@u%t5@z!c19kq4b9d9odky$5t@nt4(e%_&qmzh"
SECRET_KEY = env("DJANGO_SECRET_KEY") or INSECURE_DEFAULT_SECRET_KEY

# Hosted and installed copies are never in debug mode unless someone asks for it explicitly.
DEBUG = env_bool("DJANGO_DEBUG", not (ON_VERCEL or IS_DESKTOP))

if not DEBUG and SECRET_KEY == INSECURE_DEFAULT_SECRET_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a private random value when DJANGO_DEBUG is off."
    )

# The proxy decides what X-Forwarded-* says, so only believe it when one is really in front.
TRUST_PROXY = env_bool("TRUST_PROXY", ON_VERCEL)

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    ["localhost", "127.0.0.1"] if (DEBUG or IS_DESKTOP) else [],
)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])
if ON_VERCEL:
    for _vercel_host_var in ("VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        _vercel_host = (env(_vercel_host_var, "") or "").strip()
        if _vercel_host and _vercel_host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(_vercel_host)
        if _vercel_host and f"https://{_vercel_host}" not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(f"https://{_vercel_host}")
ADMINS = env_admins()


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "pos",
    "accounts.apps.AccountsConfig",
    "distribution.apps.DistributionConfig",
    "pricing.apps.PricingConfig",
    "django.contrib.humanize",
    "rest_framework",
    "api",
    "review.apps.ReviewConfig",
]

WHITENOISE_AVAILABLE = importlib.util.find_spec("whitenoise") is not None

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Stamps Voucher.created_by for every voucher posted during the request.
    "accounts.middleware.CurrentActorMiddleware",
    # Caches the licence ceiling and the owner's feature switches for the length of one request.
    "pos.middleware.ModuleStateMiddleware",
    "pos.middleware.LoginRequiredMiddleware",
    "pos.middleware.UserTimeZoneMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if WHITENOISE_AVAILABLE:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "possumus.urls"

# Bootstrap has no "alert-error"; without this, error messages render unstyled.
from django.contrib.messages import constants as message_constants  # noqa: E402

MESSAGE_TAGS = {message_constants.ERROR: "danger"}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "pos.context_processors.app_shell",
                "review.context_processors.review_tool",
            ],
        },
    },
]

WSGI_APPLICATION = "possumus.wsgi.application"


DATABASES = {"default": database_config()}
DATABASE_IS_SQLITE = DATABASES["default"]["ENGINE"].endswith("sqlite3")
if ON_VERCEL and DATABASE_IS_SQLITE:
    raise ImproperlyConfigured(
        "Vercel has no persistent disk: set DATABASE_URL to a hosted Postgres database (Neon, Supabase)."
    )
DATABASES["default"]["CONN_MAX_AGE"] = env_int(
    "DB_CONN_MAX_AGE", 0 if (DEBUG or ON_VERCEL or DATABASE_IS_SQLITE) else 60
)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = env_bool("DB_CONN_HEALTH_CHECKS", not DEBUG)
# Pooled connection strings (PgBouncer in transaction mode) cannot keep server-side cursors.
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = env_bool("DB_DISABLE_SERVER_SIDE_CURSORS", ON_VERCEL)


def cache_config():
    redis_url = (env("REDIS_URL", "") or "").strip()
    # Throttling and login lockout count in the cache, so serverless needs a shared one.
    default_backend = "redis" if redis_url else ("db" if ON_VERCEL else "locmem")
    backend = (env("CACHE_BACKEND") or default_backend).strip().lower()
    if backend == "locmem":
        return {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "zehnms"}
    if backend == "db":
        return {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": env("CACHE_TABLE", "zehnms_cache"),
        }
    if backend == "redis":
        if not redis_url:
            raise ImproperlyConfigured("CACHE_BACKEND=redis needs REDIS_URL.")
        if importlib.util.find_spec("redis") is None:
            raise ImproperlyConfigured("REDIS_URL is set but the 'redis' package is not installed.")
        return {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": redis_url}
    raise ImproperlyConfigured("CACHE_BACKEND must be 'locmem', 'db' or 'redis'.")


CACHES = {"default": cache_config()}


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"
# Server default only: each user picks (or the browser reports) their own timezone.
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
WHITENOISE_MAX_AGE = env_int("WHITENOISE_MAX_AGE", 31536000 if not DEBUG else 0)
WHITENOISE_AUTOREFRESH = DEBUG
# Off by default: on Windows its cleanup pass crashes on names such as "font.woff?v=4.0.3",
# which would break collectstatic for the desktop build.
WHITENOISE_KEEP_ONLY_HASHED_FILES = env_bool("WHITENOISE_KEEP_ONLY_HASHED_FILES", False)

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "possumus.storage.ForgivingManifestStaticFilesStorage"
            # Tests render plain paths, so a manifest left behind by a build cannot change their output.
            if WHITENOISE_AVAILABLE and not RUNNING_TESTS
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env("DJANGO_MEDIA_ROOT") or DATA_DIR / "media")

# Optional S3-compatible media (AWS S3, Supabase Storage, Cloudflare R2, MinIO) for hosts without a disk.
AWS_STORAGE_BUCKET_NAME = (env("AWS_STORAGE_BUCKET_NAME", "") or "").strip()
USE_S3_MEDIA = bool(AWS_STORAGE_BUCKET_NAME)
if USE_S3_MEDIA:
    if importlib.util.find_spec("storages") is None:
        raise ImproperlyConfigured("AWS_STORAGE_BUCKET_NAME is set but django-storages is not installed.")
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": AWS_STORAGE_BUCKET_NAME,
            "access_key": env("AWS_ACCESS_KEY_ID") or None,
            "secret_key": env("AWS_SECRET_ACCESS_KEY") or None,
            "region_name": env("AWS_S3_REGION_NAME") or None,
            "endpoint_url": env("AWS_S3_ENDPOINT_URL") or None,
            "custom_domain": env("AWS_S3_CUSTOM_DOMAIN") or None,
            "location": env("AWS_S3_MEDIA_PREFIX", "media"),
            "default_acl": None,
            "file_overwrite": False,
            "querystring_auth": env_bool("AWS_S3_SIGNED_URLS", True),
        },
    }

# A real web server should serve /media/; Django only does it for the installed desktop copy and in DEBUG.
SERVE_MEDIA_FILES = env_bool("DJANGO_SERVE_MEDIA", (DEBUG or IS_DESKTOP) and not USE_S3_MEDIA)

DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DJANGO_DATA_UPLOAD_MAX_MEMORY_SIZE", 10 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int("DJANGO_FILE_UPLOAD_MAX_MEMORY_SIZE", 5 * 1024 * 1024)
# Long purchase bills and carts post several inputs per line.
DATA_UPLOAD_MAX_NUMBER_FIELDS = env_int("DJANGO_DATA_UPLOAD_MAX_NUMBER_FIELDS", 5000)
MAX_LOGO_UPLOAD_BYTES = env_int("MAX_LOGO_UPLOAD_BYTES", 2 * 1024 * 1024)
# The proof of a bank payment: a photo, a screenshot or the bank's PDF slip.
MAX_RECEIPT_UPLOAD_BYTES = env_int("MAX_RECEIPT_UPLOAD_BYTES", 5 * 1024 * 1024)
MAX_BACKUP_UPLOAD_BYTES = env_int("MAX_BACKUP_UPLOAD_BYTES", 200 * 1024 * 1024)

# pg_dump/mysqldump and a persistent backups folder do not exist on serverless hosts.
BACKUP_DIR = Path(env("ZEHNMS_BACKUP_DIR") or DATA_DIR / "backups")
BACKUPS_AVAILABLE = env_bool("BACKUPS_AVAILABLE", not ON_VERCEL)
# Restoring an uploaded .sql/.dump file runs arbitrary SQL as the database user.
ALLOW_SQL_RESTORE = env_bool("ALLOW_SQL_RESTORE", False)

# The "Comment" review tool (pin a note on any part of any screen). Off unless asked for:
# test deployments and local development set ZEHNMS_REVIEW_NOTES=1. The owner can also
# switch it on or off from /review/notes/, which overrides this default.
REVIEW_NOTES_ENABLED = env_bool("ZEHNMS_REVIEW_NOTES", False)


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"
LOGIN_MAX_ATTEMPTS = env_int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCKOUT_SECONDS = env_int("LOGIN_LOCKOUT_SECONDS", 900)

# A till left open overnight signs itself out; activity keeps the 12 hours sliding.
SESSION_COOKIE_AGE = env_int("DJANGO_SESSION_COOKIE_AGE", 12 * 60 * 60)
SESSION_SAVE_EVERY_REQUEST = True

TRIAL_DAYS = env_int("TRIAL_DAYS", 10)
LICENSE_PUBLIC_KEY = (env("LICENSE_PUBLIC_KEY", "") or "").strip()
if not LICENSE_PUBLIC_KEY:
    _license_key_file = Path(env("LICENSE_PUBLIC_KEY_PATH") or BASE_DIR / "licensing" / "public_key.pem")
    if _license_key_file.is_file():
        LICENSE_PUBLIC_KEY = _license_key_file.read_text(encoding="utf-8").strip()


REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("API_THROTTLE_ANON", "30/min"),
        "user": env("API_THROTTLE_USER", "240/min"),
    },
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    # 0 = ignore X-Forwarded-For and throttle on the socket address.
    "NUM_PROXIES": env_int("NUM_PROXIES", 1) if TRUST_PROXY else 0,
}


USE_X_FORWARDED_HOST = env_bool("DJANGO_USE_X_FORWARDED_HOST", TRUST_PROXY)
if env_bool("DJANGO_USE_X_FORWARDED_PROTO", TRUST_PROXY):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# The installed desktop copy is plain http on localhost/LAN, so HTTPS-only cookies would lock everyone out.
HTTPS_EXPECTED = not DEBUG and not IS_DESKTOP

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", HTTPS_EXPECTED)
# Load balancers and uptime probes hit the health check over plain http.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", HTTPS_EXPECTED)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", HTTPS_EXPECTED)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = env_bool("DJANGO_CSRF_COOKIE_HTTPONLY", False)
SESSION_COOKIE_SAMESITE = env("DJANGO_SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = env("DJANGO_CSRF_COOKIE_SAMESITE", "Lax")
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31536000 if HTTPS_EXPECTED else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", HTTPS_EXPECTED)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", HTTPS_EXPECTED)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = env("DJANGO_SECURE_REFERRER_POLICY", "same-origin")
X_FRAME_OPTIONS = env("DJANGO_X_FRAME_OPTIONS", "DENY")


EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", "[ZehnMS] ")
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", "server@localhost")
DEFAULT_FROM_EMAIL = env("DJANGO_DEFAULT_FROM_EMAIL", "hello@example.com")
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.locmem.EmailBackend"
    if RUNNING_TESTS
    else ("django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend"),
)
EMAIL_HOST = env("DJANGO_EMAIL_HOST", "")
EMAIL_PORT = env_int("DJANGO_EMAIL_PORT", 587)
EMAIL_HOST_USER = env("DJANGO_EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("DJANGO_EMAIL_USE_TLS", not DEBUG)
EMAIL_USE_SSL = env_bool("DJANGO_EMAIL_USE_SSL", False)
EMAIL_TIMEOUT = env_int("DJANGO_EMAIL_TIMEOUT", 10)
PUBLIC_BRAND_NAME = env("PUBLIC_BRAND_NAME", "ZehnMS")
PUBLIC_APP_DOMAIN = env("PUBLIC_APP_DOMAIN", "https://example.com")
LEMON_SQUEEZY_PROVIDER_NAME = "Lemon Squeezy"
LEMON_SQUEEZY_STARTER_URL = env("LEMON_SQUEEZY_STARTER_URL", "https://zehnms.lemonsqueezy.com/buy/starter-demo").strip()
LEMON_SQUEEZY_GROWTH_URL = env("LEMON_SQUEEZY_GROWTH_URL", "https://zehnms.lemonsqueezy.com/buy/growth-demo").strip()
LEMON_SQUEEZY_SCALE_URL = env("LEMON_SQUEEZY_SCALE_URL", "https://zehnms.lemonsqueezy.com/buy/scale-demo").strip()
LEMON_SQUEEZY_WEBHOOK_SECRET = env("LEMON_SQUEEZY_WEBHOOK_SECRET", "").strip()
LEMON_SQUEEZY_SUCCESS_URL = env("LEMON_SQUEEZY_SUCCESS_URL", "").strip()
LEMON_SQUEEZY_CANCEL_URL = env("LEMON_SQUEEZY_CANCEL_URL", "").strip()
LEMON_SQUEEZY_CUSTOMER_PORTAL_URL = env("LEMON_SQUEEZY_CUSTOMER_PORTAL_URL", "").strip()
# Maps Lemon Squeezy variant ids to plan codes, e.g. {"123456": "starter", "123457": "growth"}.
LEMON_SQUEEZY_VARIANT_PLAN_MAP = env_json("LEMON_SQUEEZY_VARIANT_PLAN_MAP", {})
if not isinstance(LEMON_SQUEEZY_VARIANT_PLAN_MAP, dict):
    raise ImproperlyConfigured("LEMON_SQUEEZY_VARIANT_PLAN_MAP must be a JSON object.")
LEMON_SQUEEZY_VARIANT_PLAN_MAP = {str(key): str(value) for key, value in LEMON_SQUEEZY_VARIANT_PLAN_MAP.items()}
# The installed single-shop copy is licensed offline, never through online checkout.
LEMON_SQUEEZY_ENABLED = env_bool(
    "LEMON_SQUEEZY_ENABLED",
    not IS_DESKTOP and bool(LEMON_SQUEEZY_STARTER_URL or LEMON_SQUEEZY_GROWTH_URL or LEMON_SQUEEZY_SCALE_URL),
)
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_REDIRECT_URI = env("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
GOOGLE_OAUTH_AUTHORIZE_URL = env("GOOGLE_OAUTH_AUTHORIZE_URL", "https://accounts.google.com/o/oauth2/v2/auth").strip()
GOOGLE_OAUTH_TOKEN_URL = env("GOOGLE_OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token").strip()
GOOGLE_OAUTH_SCOPE = env("GOOGLE_OAUTH_SCOPE", "openid email profile").strip()
GOOGLE_OAUTH_TIMEOUT = env_int("GOOGLE_OAUTH_TIMEOUT", 10)
GOOGLE_SIGN_IN_ENABLED = env_bool(
    "GOOGLE_SIGN_IN_ENABLED",
    bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET),
)

LOG_DIR = Path(env("DJANGO_LOG_DIR") or DATA_DIR / "logs")
LOG_LEVEL = env("DJANGO_LOG_LEVEL", "INFO").upper()

# Serverless and read-only installs log to the console only (the host collects stdout).
ENABLE_FILE_LOGGING = (
    env_bool("DJANGO_FILE_LOGGING", not RUNNING_TESTS and not ON_VERCEL)
    and _ensure_writable_dir(LOG_DIR)
)
base_log_handlers = ["console"] + (["application_file"] if ENABLE_FILE_LOGGING else [])
request_handlers = list(base_log_handlers)
if ADMINS and not DEBUG:
    request_handlers.append("mail_admins")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
    },
    "formatters": {
        "verbose": {
            "format": "%(levelname)s %(asctime)s %(name)s %(module)s:%(lineno)d %(message)s",
        },
        "simple": {
            "format": "%(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "level": LOG_LEVEL,
        },
        "mail_admins": {
            "class": "django.utils.log.AdminEmailHandler",
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "include_html": True,
        },
    },
    "root": {
        "handlers": base_log_handlers,
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": base_log_handlers,
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": request_handlers,
            "level": "ERROR",
            "propagate": False,
        },
        "django.security.DisallowedHost": {
            "handlers": request_handlers,
            "level": "ERROR",
            "propagate": False,
        },
        "accounts": {
            "handlers": base_log_handlers,
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "api": {
            "handlers": base_log_handlers,
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "pos": {
            "handlers": base_log_handlers,
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}


# dictConfig opens every declared handler, so the file handler only exists when it can be written.
if ENABLE_FILE_LOGGING:
    LOGGING["handlers"]["application_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "verbose",
        "level": LOG_LEVEL,
        "filename": str(LOG_DIR / "application.log"),
        "maxBytes": env_int("DJANGO_LOG_FILE_MAX_BYTES", 5 * 1024 * 1024),
        "backupCount": env_int("DJANGO_LOG_FILE_BACKUP_COUNT", 5),
        "encoding": "utf-8",
        "delay": True,
    }


SENTRY_DSN = env("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
    except ImportError as exc:
        raise ImproperlyConfigured(
            "SENTRY_DSN is set but sentry-sdk is not installed (pip install sentry-sdk)."
        ) from exc

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", "development" if DEBUG else "production"),
        release=env("SENTRY_RELEASE", ""),
        send_default_pii=env_bool("SENTRY_SEND_DEFAULT_PII", False),
        traces_sample_rate=env_float("SENTRY_TRACES_SAMPLE_RATE", 0.0),
        profiles_sample_rate=env_float("SENTRY_PROFILES_SAMPLE_RATE", 0.0),
    )



