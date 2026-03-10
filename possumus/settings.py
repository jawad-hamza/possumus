import importlib.util
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured


BASE_DIR = Path(__file__).resolve().parent.parent
RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == "test"

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


def _install_pymysql():
    try:
        import pymysql
    except ImportError as exc:
        raise ImproperlyConfigured(
            "PyMySQL is required when DB_ENGINE/mysql DATABASE_URL points to MySQL."
        ) from exc
    pymysql.install_as_MySQLdb()


def _build_sqlite_config(db_name=None):
    sqlite_name = db_name or env("SQLITE_PATH") or str(BASE_DIR / "db.sqlite3")
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": sqlite_name,
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
            options = {}
            if query.get("sslmode"):
                options["sslmode"] = query["sslmode"][-1]
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


SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    "django-insecure-%kju_qo0cwbk@u%t5@z!c19kq4b9d9odky$5t@nt4(e%_&qmzh",
)

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    ["localhost", "127.0.0.1"] if DEBUG else [],
)
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])
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
    "django.contrib.humanize",
    "rest_framework",
    "api",
]

WHITENOISE_AVAILABLE = importlib.util.find_spec("whitenoise") is not None

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

if WHITENOISE_AVAILABLE:
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "possumus.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "possumus.wsgi.application"


DATABASES = {"default": database_config()}
DATABASES["default"]["CONN_MAX_AGE"] = env_int("DB_CONN_MAX_AGE", 0 if DEBUG else 60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = env_bool("DB_CONN_HEALTH_CHECKS", not DEBUG)


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
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Karachi")
USE_I18N = True
USE_TZ = True


STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
WHITENOISE_MAX_AGE = env_int("WHITENOISE_MAX_AGE", 31536000 if not DEBUG else 0)
WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_KEEP_ONLY_HASHED_FILES = not DEBUG

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if WHITENOISE_AVAILABLE
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"


USE_X_FORWARDED_HOST = env_bool("DJANGO_USE_X_FORWARDED_HOST", not DEBUG)
if env_bool("DJANGO_USE_X_FORWARDED_PROTO", not DEBUG):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = env_bool("DJANGO_CSRF_COOKIE_HTTPONLY", False)
SESSION_COOKIE_SAMESITE = env("DJANGO_SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = env("DJANGO_CSRF_COOKIE_SAMESITE", "Lax")
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", not DEBUG)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = env("DJANGO_SECURE_REFERRER_POLICY", "same-origin")
X_FRAME_OPTIONS = env("DJANGO_X_FRAME_OPTIONS", "DENY")


EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", "[Possumus] ")
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", "server@localhost")

LOG_DIR = Path(env("DJANGO_LOG_DIR", str(BASE_DIR / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_LEVEL = env("DJANGO_LOG_LEVEL", "INFO").upper()

ENABLE_FILE_LOGGING = env_bool("DJANGO_FILE_LOGGING", not RUNNING_TESTS)
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
        "application_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "verbose",
            "level": LOG_LEVEL,
            "filename": str(LOG_DIR / "application.log"),
            "maxBytes": env_int("DJANGO_LOG_FILE_MAX_BYTES", 5 * 1024 * 1024),
            "backupCount": env_int("DJANGO_LOG_FILE_BACKUP_COUNT", 5),
            "encoding": "utf-8",
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


SENTRY_DSN = env("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration
    except ImportError as exc:
        raise ImproperlyConfigured(
            "SENTRY_DSN is set but sentry-sdk is not installed. Add it from requirements.txt."
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



