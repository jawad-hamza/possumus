"""Platform tests: settings behaviour per deployment target, health check, packaging files."""

import ast
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.sessions.models import Session
from django.test import SimpleTestCase, TestCase
from django.urls import resolve, reverse

from pos.middleware import LoginRequiredMiddleware


REPO_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = REPO_ROOT / "possumus" / "settings.py"
STRONG_KEY = "k" * 64

# Variables that would leak from the developer's shell into the child interpreter.
_SETTINGS_ENV_PREFIXES = (
    "DJANGO_", "DB_", "ZEHNMS_", "VERCEL", "AWS_", "LEMON_", "GOOGLE_", "SENTRY_", "CACHE_", "LOGIN_", "API_",
)
_SETTINGS_ENV_NAMES = {
    "DATABASE_URL", "DEPLOYMENT_MODE", "SQLITE_PATH", "REDIS_URL", "TRUST_PROXY", "NUM_PROXIES", "TRIAL_DAYS",
    "LICENSE_PUBLIC_KEY", "LICENSE_PUBLIC_KEY_PATH", "ALLOW_SQL_RESTORE", "BACKUPS_AVAILABLE",
}

_DUMP_SETTINGS = """
import json
from possumus import settings as s
names = [name for name in dir(s) if name.isupper()]
print("SETTINGS_JSON=" + json.dumps({name: getattr(s, name) for name in names}, default=str))
"""


def load_settings_in_subprocess(**overrides):
    """Import possumus.settings in a clean interpreter, the way a fresh deployment would."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_SETTINGS_ENV_PREFIXES) and key not in _SETTINGS_ENV_NAMES
    }
    # Point at a file that does not exist so a developer's .env cannot influence the result.
    env["ZEHNMS_ENV_FILE"] = str(REPO_ROOT / "__no_such_env_file__")
    env.update(overrides)
    completed = subprocess.run(
        [sys.executable, "-c", _DUMP_SETTINGS],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    loaded = None
    for line in completed.stdout.splitlines():
        if line.startswith("SETTINGS_JSON="):
            loaded = json.loads(line[len("SETTINGS_JSON="):])
    return completed, loaded


def load_launcher_module():
    spec = importlib.util.spec_from_file_location("zehnms_desktop_launcher", REPO_ROOT / "desktop" / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HealthCheckTests(TestCase):
    def test_route_name_and_path_are_stable(self):
        self.assertEqual(reverse("health_check"), "/healthz/")
        self.assertEqual(resolve("/healthz/").url_name, "health_check")
        self.assertIn("health_check", LoginRequiredMiddleware.PUBLIC_URL_NAMES)

    def test_anonymous_probe_gets_json_without_creating_a_session(self):
        response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"status": "ok", "db": "ok"})
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(Session.objects.count(), 0)
        self.assertIn("no-store", response["Cache-Control"])

    def test_plain_http_probe_is_not_redirected_to_https(self):
        with self.settings(SECURE_SSL_REDIRECT=True):
            response = self.client.get("/healthz/", secure=False)
        self.assertEqual(response.status_code, 200)

    def test_head_is_allowed_and_post_is_not(self):
        self.assertEqual(self.client.head("/healthz/").status_code, 200)
        self.assertEqual(self.client.post("/healthz/").status_code, 405)

    def test_database_failure_reports_503_without_leaking_details(self):
        with mock.patch("pos.views.health.connection.cursor", side_effect=RuntimeError("secret dsn in message")):
            with self.assertLogs("pos.views.health", level="ERROR"):
                response = self.client.get("/healthz/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error", "db": "unavailable"})
        self.assertNotIn("secret dsn", response.content.decode())


class ActiveSettingsTests(SimpleTestCase):
    def test_api_is_session_only_authenticated_and_throttled(self):
        config = settings.REST_FRAMEWORK
        self.assertEqual(
            config["DEFAULT_AUTHENTICATION_CLASSES"],
            ["rest_framework.authentication.SessionAuthentication"],
        )
        self.assertEqual(config["DEFAULT_PERMISSION_CLASSES"], ["rest_framework.permissions.IsAuthenticated"])
        self.assertEqual(
            config["DEFAULT_THROTTLE_CLASSES"],
            ["rest_framework.throttling.AnonRateThrottle", "rest_framework.throttling.UserRateThrottle"],
        )
        self.assertEqual(set(config["DEFAULT_THROTTLE_RATES"]), {"anon", "user"})
        self.assertIn("rest_framework.renderers.JSONRenderer", config["DEFAULT_RENDERER_CLASSES"])

    def test_session_is_twelve_hours_sliding(self):
        self.assertEqual(settings.SESSION_COOKIE_AGE, 12 * 60 * 60)
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)

    def test_upload_limits_and_cache_are_configured(self):
        self.assertLessEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 10 * 1024 * 1024)
        self.assertLessEqual(settings.FILE_UPLOAD_MAX_MEMORY_SIZE, 5 * 1024 * 1024)
        self.assertIn("default", settings.CACHES)

    def test_media_is_served_by_django_only_when_enabled(self):
        if settings.SERVE_MEDIA_FILES:
            self.assertEqual(resolve("/media/company_logos/logo.png").kwargs["path"], "company_logos/logo.png")

    def test_env_file_loader_never_overrides_real_environment(self):
        from possumus.settings import _load_env_file

        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text(
                "# comment\nexport PLATFORM_TEST_A=from-file\nPLATFORM_TEST_B='quoted value'\nPLATFORM_TEST_C=kept\nbroken line\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"PLATFORM_TEST_C": "from-shell"}):
                _load_env_file(env_file)
                self.assertEqual(os.environ["PLATFORM_TEST_A"], "from-file")
                self.assertEqual(os.environ["PLATFORM_TEST_B"], "quoted value")
                self.assertEqual(os.environ["PLATFORM_TEST_C"], "from-shell")
                _load_env_file(Path(folder) / "missing.env")


class SettingsPerDeploymentTargetTests(SimpleTestCase):
    def test_local_development_defaults(self):
        completed, loaded = load_settings_in_subprocess()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(loaded["DEBUG"])
        self.assertEqual(loaded["DEPLOYMENT_MODE"], "cloud")
        self.assertEqual(loaded["TIME_ZONE"], "UTC")
        self.assertEqual(loaded["TRIAL_DAYS"], 10)
        self.assertEqual(loaded["LOGIN_MAX_ATTEMPTS"], 5)
        self.assertEqual(loaded["LOGIN_LOCKOUT_SECONDS"], 900)
        self.assertEqual(loaded["LEMON_SQUEEZY_VARIANT_PLAN_MAP"], {})
        self.assertFalse(loaded["ALLOW_SQL_RESTORE"])
        self.assertFalse(loaded["TRUST_PROXY"])
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", loaded)
        self.assertIn("locmem", loaded["CACHES"]["default"]["BACKEND"])

    def test_production_refuses_the_insecure_default_secret_key(self):
        completed, loaded = load_settings_in_subprocess(DJANGO_DEBUG="false")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIsNone(loaded)
        self.assertIn("DJANGO_SECRET_KEY", completed.stderr)

    def test_production_with_a_real_key_turns_https_hardening_on(self):
        completed, loaded = load_settings_in_subprocess(
            DJANGO_DEBUG="false", DJANGO_SECRET_KEY=STRONG_KEY, DJANGO_ALLOWED_HOSTS="shop.example.com", TRUST_PROXY="true"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(loaded["DEBUG"])
        self.assertTrue(loaded["SECURE_SSL_REDIRECT"])
        self.assertTrue(loaded["SESSION_COOKIE_SECURE"])
        self.assertTrue(loaded["CSRF_COOKIE_SECURE"])
        self.assertEqual(loaded["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertFalse(loaded["SERVE_MEDIA_FILES"])
        self.assertEqual(loaded["REST_FRAMEWORK"]["DEFAULT_RENDERER_CLASSES"], ["rest_framework.renderers.JSONRenderer"])

    def test_desktop_mode_keeps_all_data_under_the_data_dir_and_works_over_http(self):
        with tempfile.TemporaryDirectory() as folder:
            completed, loaded = load_settings_in_subprocess(
                DEPLOYMENT_MODE="desktop", ZEHNMS_DATA_DIR=folder, DJANGO_SECRET_KEY=STRONG_KEY
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            data_dir = Path(folder)
            self.assertFalse(loaded["DEBUG"])
            self.assertEqual(Path(loaded["DATABASES"]["default"]["NAME"]), data_dir / "db.sqlite3")
            self.assertEqual(Path(loaded["MEDIA_ROOT"]), data_dir / "media")
            self.assertEqual(Path(loaded["LOG_DIR"]), data_dir / "logs")
            self.assertEqual(Path(loaded["BACKUP_DIR"]), data_dir / "backups")
            self.assertEqual(loaded["ALLOWED_HOSTS"], ["localhost", "127.0.0.1"])
            self.assertFalse(loaded["SECURE_SSL_REDIRECT"])
            self.assertFalse(loaded["SESSION_COOKIE_SECURE"])
            self.assertFalse(loaded["CSRF_COOKIE_SECURE"])
            self.assertEqual(loaded["SECURE_HSTS_SECONDS"], 0)
            self.assertTrue(loaded["SERVE_MEDIA_FILES"])
            self.assertFalse(loaded["LEMON_SQUEEZY_ENABLED"])
            self.assertTrue(loaded["BACKUPS_AVAILABLE"])
            self.assertIn("WAL", loaded["DATABASES"]["default"]["OPTIONS"]["init_command"])

    def test_vercel_requires_a_hosted_database(self):
        completed, loaded = load_settings_in_subprocess(VERCEL="1", DJANGO_SECRET_KEY=STRONG_KEY)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("DATABASE_URL", completed.stderr)

    def test_vercel_never_touches_the_read_only_filesystem(self):
        completed, loaded = load_settings_in_subprocess(
            VERCEL="1",
            VERCEL_URL="zehnms-abc123.vercel.app",
            VERCEL_PROJECT_PRODUCTION_URL="shop.example.com",
            DJANGO_SECRET_KEY=STRONG_KEY,
            DATABASE_URL="postgresql://user:pass@ep-cool-1234-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(loaded["DEBUG"])
        self.assertFalse(loaded["ENABLE_FILE_LOGGING"])
        self.assertNotIn("application_file", loaded["LOGGING"]["handlers"])
        self.assertEqual(loaded["LOGGING"]["root"]["handlers"], ["console"])
        self.assertFalse(loaded["BACKUPS_AVAILABLE"])
        self.assertTrue(Path(loaded["MEDIA_ROOT"]).as_posix().startswith("/tmp/"))
        self.assertIn("zehnms-abc123.vercel.app", loaded["ALLOWED_HOSTS"])
        self.assertIn("https://shop.example.com", loaded["CSRF_TRUSTED_ORIGINS"])
        self.assertTrue(loaded["TRUST_PROXY"])
        database = loaded["DATABASES"]["default"]
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["NAME"], "neondb")
        self.assertEqual(database["OPTIONS"], {"sslmode": "require", "channel_binding": "require"})
        self.assertEqual(database["CONN_MAX_AGE"], 0)
        self.assertTrue(database["DISABLE_SERVER_SIDE_CURSORS"])
        self.assertIn("DatabaseCache", loaded["CACHES"]["default"]["BACKEND"])

    def test_unwritable_log_folder_falls_back_to_console_logging(self):
        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "not-a-folder"
            blocker.write_text("x", encoding="utf-8")
            completed, loaded = load_settings_in_subprocess(DJANGO_LOG_DIR=str(blocker / "logs"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(loaded["ENABLE_FILE_LOGGING"])
        self.assertNotIn("application_file", loaded["LOGGING"]["handlers"])

    def test_variant_plan_map_is_parsed_from_json_and_rejects_garbage(self):
        completed, loaded = load_settings_in_subprocess(LEMON_SQUEEZY_VARIANT_PLAN_MAP='{"123": "starter", "456": "growth"}')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(loaded["LEMON_SQUEEZY_VARIANT_PLAN_MAP"], {"123": "starter", "456": "growth"})

        completed, _ = load_settings_in_subprocess(LEMON_SQUEEZY_VARIANT_PLAN_MAP="{not json")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("LEMON_SQUEEZY_VARIANT_PLAN_MAP", completed.stderr)

    def test_s3_media_is_switched_on_by_the_bucket_name(self):
        completed, loaded = load_settings_in_subprocess(
            AWS_STORAGE_BUCKET_NAME="zehnms-media", AWS_S3_ENDPOINT_URL="https://project.supabase.co/storage/v1/s3"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        storage = loaded["STORAGES"]["default"]
        self.assertEqual(storage["BACKEND"], "storages.backends.s3.S3Storage")
        self.assertEqual(storage["OPTIONS"]["bucket_name"], "zehnms-media")
        self.assertFalse(storage["OPTIONS"]["file_overwrite"])
        self.assertFalse(loaded["SERVE_MEDIA_FILES"])


def _env_names_read_by_settings():
    readers = {"env", "env_bool", "env_int", "env_float", "env_list", "env_json", "env_admins", "getenv"}
    names = set()
    for node in ast.walk(ast.parse(SETTINGS_FILE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            function_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if function_name in readers and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
        # Defaults such as env_admins(name="DJANGO_ADMINS") and the VERCEL_* host tuple.
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("VERCEL_") or node.value == "DJANGO_ADMINS":
                names.add(node.value)
    return names


class RepositoryHygieneTests(SimpleTestCase):
    def test_env_example_documents_every_variable_settings_reads(self):
        documented = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        names = _env_names_read_by_settings()
        self.assertGreater(len(names), 80)
        # The three VERCEL_* host variables are injected by the platform, not configured by people.
        platform_provided = {"VERCEL_URL", "VERCEL_BRANCH_URL", "VERCEL_PROJECT_PRODUCTION_URL"}
        missing = sorted(name for name in names - platform_provided if f"{name}=" not in documented)
        self.assertEqual(missing, [])

    def test_requirements_are_pinned_cross_platform_and_list_what_the_app_imports(self):
        lines = [
            line.strip()
            for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        packages = {line.split("==")[0].split("[")[0].lower() for line in lines}

        self.assertTrue(all("==" in line for line in lines), lines)
        for required in ("django", "djangorestframework", "reportlab", "python-barcode", "pillow", "requests",
                         "google-auth", "cryptography", "whitenoise", "psycopg", "tzdata"):
            self.assertIn(required, packages)
        for windows_only_or_unrelated in ("pywin32", "pypiwin32", "pyautogui", "comtypes", "flask", "chromadb"):
            self.assertNotIn(windows_only_or_unrelated, packages)

        desktop = (REPO_ROOT / "requirements-desktop.txt").read_text(encoding="utf-8")
        self.assertIn("-r requirements.txt", desktop)
        self.assertIn("waitress==", desktop)
        self.assertIn("pyinstaller==", desktop)
        # The standalone window: pywebview needs pythonnet on Windows, and neither belongs on the server.
        self.assertIn("pywebview==", desktop)
        self.assertIn("pythonnet==", desktop)
        self.assertNotIn("pywebview", packages)
        dev =(REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertIn("pyflakes==", dev)
        self.assertIn("ruff==", dev)

    def test_vercel_configuration_points_at_the_real_wsgi_module(self):
        config = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))
        wsgi_file = settings.WSGI_APPLICATION.rsplit(".", 1)[0].replace(".", "/") + ".py"

        # The Django preset routes every request to the app, so the api/ Django app never
        # becomes a set of file-based serverless functions.
        self.assertEqual(config["framework"], "django")
        self.assertNotIn("builds", config)
        self.assertIn(wsgi_file, config["functions"])
        self.assertTrue((REPO_ROOT / wsgi_file).is_file())
        self.assertLessEqual(len(config["functions"][wsgi_file]["excludeFiles"]), 256)
        build_script = config["buildCommand"].split()[-1]
        self.assertTrue((REPO_ROOT / build_script).is_file())
        self.assertIn((REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip(), {"3.12", "3.13", "3.14"})

    def test_vercel_build_migrates_production_only_unless_told_otherwise(self):
        spec = importlib.util.spec_from_file_location("zehnms_vercel_build", REPO_ROOT / "scripts" / "vercel_build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with mock.patch.dict(os.environ, {"VERCEL_ENV": "production"}, clear=True):
            self.assertTrue(module.should_migrate())
        with mock.patch.dict(os.environ, {"VERCEL_ENV": "preview"}, clear=True):
            self.assertFalse(module.should_migrate())
        with mock.patch.dict(os.environ, {"VERCEL_ENV": "preview", "ZEHNMS_MIGRATE_ON_BUILD": "1"}, clear=True):
            self.assertTrue(module.should_migrate())
        with mock.patch.dict(os.environ, {"VERCEL_ENV": "production", "ZEHNMS_MIGRATE_ON_BUILD": "0"}, clear=True):
            self.assertFalse(module.should_migrate())

    def test_secrets_and_build_output_are_git_ignored(self):
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in (".venv/", ".env", "staticfiles/", "dist/", "build/", "desktop/dist/",
                        "licensing/private_key.pem", "*.spec", "!desktop/zehnms.spec", "!.env.example"):
            self.assertIn(pattern, ignored)

    def test_desktop_packaging_files_exist(self):
        for name in ("launcher.py", "zehnms.spec", "installer.iss", "build.ps1", "README.md", "VERSION",
                     "make_icon.py", "zehnms.ico"):
            self.assertTrue((REPO_ROOT / "desktop" / name).is_file(), name)
        installer = (REPO_ROOT / "desktop" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("PrivilegesRequired=lowest", installer)
        self._assert_window_packaging(installer)
        # The uninstaller may clean the program folder but must never name the shop's data folder.
        uninstall_section = installer.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
        active_lines = [line for line in uninstall_section.splitlines() if line.strip() and not line.strip().startswith(";")]
        self.assertTrue(all("{app}" in line for line in active_lines), active_lines)
        self.assertFalse(any("{localappdata}" in line.lower() for line in active_lines))

    def _assert_window_packaging(self, installer):
        """The installed copy opens as its own window: icon, taskbar identity and WebView2 handling agree."""
        launcher = load_launcher_module()
        # Setup looks for the WebView2 runtime under the same registry id the launcher reads.
        self.assertIn(launcher.WEBVIEW2_CLIENT_GUID, installer)
        self.assertIn("WebView2Missing", installer)
        self.assertIn("MicrosoftEdgeWebview2Setup.exe", installer)
        # A pinned shortcut and the running window share one taskbar button only if the ids match.
        self.assertIn(f'#define AppUserModelId "{launcher.APP_USER_MODEL_ID}"', installer)
        self.assertIn("SetupIconFile=zehnms.ico", installer)

        spec = (REPO_ROOT / "desktop" / "zehnms.spec").read_text(encoding="utf-8")
        self.assertIn("zehnms.ico", spec)
        self.assertIn("webview.platforms.edgechromium", spec)
        build_script = (REPO_ROOT / "desktop" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("FetchWebView2", build_script)
        self.assertIn("--self-test", build_script)
        # The bootstrapper is fetched from Microsoft at build time, never committed.
        self.assertIn("desktop/redist/", (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        with open(REPO_ROOT / "desktop" / "zehnms.ico", "rb") as icon:
            self.assertEqual(icon.read(4), b"\x00\x00\x01\x00")


class DesktopLauncherTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.launcher = load_launcher_module()

    def test_data_folder_defaults_to_local_app_data_and_can_be_overridden(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\shop\AppData\Local"}, clear=True):
            self.assertEqual(self.launcher.default_data_dir(), Path(r"C:\Users\shop\AppData\Local") / "ZehnMS")
        with mock.patch.dict(os.environ, {"ZEHNMS_DATA_DIR": "/srv/till", "LOCALAPPDATA": "x"}, clear=True):
            self.assertEqual(self.launcher.default_data_dir(), Path("/srv/till"))

    def test_secret_key_is_generated_once_and_then_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            first = self.launcher.load_or_create_secret_key(Path(folder))
            second = self.launcher.load_or_create_secret_key(Path(folder))

        self.assertGreaterEqual(len(first), 50)
        self.assertEqual(first, second)
        self.assertNotEqual(first, settings.INSECURE_DEFAULT_SECRET_KEY)

    def test_environment_is_forced_into_safe_desktop_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            polluted = {"DATABASE_URL": "postgresql://elsewhere/db", "DJANGO_DEBUG": "true", "VERCEL": "1"}
            with mock.patch.dict(os.environ, polluted), mock.patch.object(sys, "path", list(sys.path)):
                self.launcher.configure_environment(Path(folder), allowed_hosts=["localhost", "127.0.0.1", "192.168.1.20"])

                self.assertEqual(os.environ["DEPLOYMENT_MODE"], "desktop")
                self.assertEqual(os.environ["DJANGO_DEBUG"], "false")
                self.assertEqual(os.environ["ZEHNMS_DATA_DIR"], folder)
                self.assertEqual(os.environ["DJANGO_ALLOWED_HOSTS"], "localhost,127.0.0.1,192.168.1.20")
                self.assertGreaterEqual(len(os.environ["DJANGO_SECRET_KEY"]), 50)
                self.assertNotIn("DATABASE_URL", os.environ)
                self.assertNotIn("VERCEL", os.environ)
            for name in ("media", "logs", "backups"):
                self.assertTrue((Path(folder) / name).is_dir())

    def test_lock_file_round_trip_and_stale_lock_detection(self):
        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)
            self.assertIsNone(self.launcher.read_lock(data_dir))
            self.launcher.write_lock(data_dir, port=8765, lan_urls=["http://192.168.1.20:8765/"])
            lock = self.launcher.read_lock(data_dir)

            self.assertEqual(lock["port"], 8765)
            self.assertEqual(lock["pid"], os.getpid())
            self.assertEqual(lock["url"], "http://127.0.0.1:8765/")
            # Until a window opens the lock names none; a second launch reads these to find it.
            self.assertEqual((lock["window"], lock["window_pid"]), ("", None))
            self.launcher.update_lock(data_dir, window="webview", window_pid=4321)
            lock = self.launcher.read_lock(data_dir)
            self.assertEqual((lock["window"], lock["window_pid"], lock["port"]), ("webview", 4321, 8765))
            # Nothing answers /healthz/ there, so the lock is treated as left over from a crash.
            with mock.patch.object(self.launcher, "is_healthy", return_value=False):
                self.assertIsNone(self.launcher.running_instance(data_dir))
            with mock.patch.object(self.launcher, "is_healthy", return_value=True):
                self.assertEqual(self.launcher.running_instance(data_dir)["port"], 8765)

    def test_safety_backup_copies_the_database_and_keeps_only_the_newest(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "db.sqlite3"
            connection = sqlite3.connect(str(database))
            connection.execute("CREATE TABLE sale (id INTEGER PRIMARY KEY, total TEXT)")
            connection.execute("INSERT INTO sale (total) VALUES ('12.500')")
            connection.commit()
            connection.close()
            backup_dir = Path(folder) / "backups"
            backup_dir.mkdir()
            for index in range(7):
                (backup_dir / f"{self.launcher.SAFETY_BACKUP_PREFIX}2000010{index}-000000.sqlite3").write_bytes(b"old")

            target = self.launcher.take_safety_backup(database, backup_dir)

            copied = sqlite3.connect(str(target))
            try:
                self.assertEqual(copied.execute("SELECT total FROM sale").fetchall(), [("12.500",)])
            finally:
                copied.close()
            remaining = sorted(path.name for path in backup_dir.iterdir())
            self.assertEqual(len(remaining), self.launcher.SAFETY_BACKUPS_TO_KEEP)
            self.assertIn(target.name, remaining)

    def test_command_line_flags(self):
        arguments = self.launcher.parse_arguments(["--lan", "--port", "9000", "--no-browser", "--allow-host", "till-1"])
        self.assertTrue(arguments.lan)
        self.assertEqual(arguments.port, 9000)
        self.assertTrue(arguments.no_browser)
        self.assertEqual(arguments.allow_host, ["till-1"])
        self.assertFalse(arguments.stop)
        self.assertTrue(self.launcher.parse_arguments(["--stop"]).stop)

        arguments = self.launcher.parse_arguments(["--window", "appmode", "--debug"])
        self.assertEqual(arguments.window, "appmode")
        self.assertTrue(arguments.debug)
        self.assertFalse(arguments.no_browser)
        self.assertTrue(self.launcher.parse_arguments(["--no-window"]).no_browser)
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr"):
            self.launcher.parse_arguments(["--window", "netscape"])

    def test_window_strategy_falls_through_in_a_fixed_order(self):
        chain = self.launcher.window_strategy_chain
        self.assertEqual(chain("auto"), ["webview", "appmode", "browser"])
        self.assertEqual(chain(""), ["webview", "appmode", "browser"])
        # Forcing a strategy moves the starting point; it never climbs back up to a better one.
        self.assertEqual(chain("webview"), ["webview", "appmode", "browser"])
        self.assertEqual(chain("AppMode "), ["appmode", "browser"])
        self.assertEqual(chain("browser"), ["browser"])
        self.assertEqual(chain("none"), [])
        with mock.patch.object(self.launcher, "say"):
            self.assertEqual(chain("netscape"), ["webview", "appmode", "browser"])

    def test_window_strategy_comes_from_flag_then_environment_then_config(self):
        requested = self.launcher.requested_window_strategy
        parse = self.launcher.parse_arguments
        with mock.patch.dict(os.environ, {"ZEHNMS_WINDOW": "appmode"}):
            self.assertEqual(requested(parse(["--window", "browser"]), {"window": "webview"}), "browser")
            self.assertEqual(requested(parse([]), {"window": "webview"}), "appmode")
            self.assertEqual(requested(parse(["--no-window"]), {}), "none")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(requested(parse([]), {"window": "webview"}), "webview")
            self.assertEqual(requested(parse([]), {}), "auto")
            # config.json written for the older browser-only launcher keeps working.
            self.assertEqual(requested(parse([]), {"open_browser": False}), "none")

    def test_window_geometry_is_remembered_and_kept_on_screen(self):
        plan = self.launcher.plan_window
        big_screen = (1920, 1080, 0, 0, 1920, 1080)
        shop_laptop = (1366, 768, 0, 0, 1366, 768)

        first_run = plan({}, big_screen)
        self.assertEqual((first_run["width"], first_run["height"]), self.launcher.DEFAULT_WINDOW_SIZE)
        self.assertEqual((first_run["x"], first_run["y"], first_run["maximized"]), (None, None, False))
        # No room around the default size on the usual shop laptop: start maximised.
        self.assertTrue(plan({}, shop_laptop)["maximized"])
        self.assertFalse(plan({"maximized": False}, shop_laptop)["maximized"])

        saved = {"x": 200, "y": 120, "width": 1500, "height": 900, "maximized": False}
        self.assertEqual(plan(saved, big_screen), saved)
        # The second monitor it was on is gone: centre it instead of opening off-screen.
        self.assertEqual(plan({**saved, "x": 2600}, big_screen)["x"], None)
        self.assertEqual(plan({**saved, "x": 2600}, big_screen)["y"], None)
        with_second_monitor = (1920, 1080, 0, 0, 3840, 1080)
        self.assertEqual(plan({**saved, "x": 2600}, with_second_monitor)["x"], 2600)

        tiny = plan({"width": 300, "height": 200, "x": "left", "maximized": "yes"}, big_screen)
        self.assertEqual((tiny["width"], tiny["height"]), self.launcher.MIN_WINDOW_SIZE)
        self.assertEqual((tiny["x"], tiny["maximized"]), (None, False))
        self.assertEqual(plan({}, None)["maximized"], False)

        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.launcher.load_window_state(Path(folder)), {})
            self.launcher.save_window_state(Path(folder), saved)
            self.assertEqual(self.launcher.load_window_state(Path(folder)), saved)
            (Path(folder) / self.launcher.WINDOW_STATE_FILE_NAME).write_text("not json", encoding="utf-8")
            self.assertEqual(self.launcher.load_window_state(Path(folder)), {})

    def test_server_threads_fit_one_till_plus_an_optional_lan_till(self):
        count = self.launcher.server_thread_count
        self.assertEqual(count(False), 8)
        self.assertEqual(count(True), 12)
        self.assertEqual(count(False, "16"), 16)
        self.assertEqual(count(False, 500), 32)
        self.assertEqual(count(False, 1), 4)
        self.assertEqual(count(True, "many"), 12)

    def test_stop_request_wakes_the_running_copy_without_a_busy_loop(self):
        import threading

        with tempfile.TemporaryDirectory() as folder:
            data_dir = Path(folder)
            if os.name == "nt":
                # Nothing is running on this data folder, so there is nobody to deliver the request to.
                self.assertFalse(self.launcher.StopSignal.request(data_dir))
            signal = self.launcher.StopSignal(data_dir)
            woke = threading.Event()
            waiter = threading.Thread(target=lambda: (signal.wait(), woke.set()), daemon=True)
            waiter.start()
            self.assertFalse(woke.wait(0.3))
            self.assertTrue(self.launcher.StopSignal.request(data_dir))
            self.assertTrue(woke.wait(5))
            if signal.handle:
                self.launcher._kernel32().CloseHandle(signal.handle)
            # Two data folders (two shops on one PC, or the smoke test) never stop each other.
            self.assertNotEqual(
                self.launcher.StopSignal.event_name(data_dir), self.launcher.StopSignal.event_name(data_dir / "other")
            )

    def test_server_loop_ends_on_request_from_another_thread(self):
        import threading

        from waitress.server import create_server

        def application(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]

        with tempfile.TemporaryDirectory() as folder:
            shop = self.launcher.ShopServer(Path(folder), lan_mode=False, preferred_port=0)
            shop.server = create_server(application, host="127.0.0.1", port=0, threads=1)
            loop = threading.Thread(target=shop._serve, daemon=True)
            loop.start()
            shop.request_shutdown()
            loop.join(10)
            self.assertFalse(loop.is_alive())
            shop.server.task_dispatcher.shutdown(timeout=5)
            shop.request_shutdown()  # asking twice (window closed, then --stop) is harmless

    def test_second_launch_only_trusts_a_window_that_looks_like_ours(self):
        windows = [(101, "WindowsForms10.Window.8.app", "ZehnMS"), (102, "Chrome_WidgetWin_1", "Sign In | ZehnMS"),
                   (103, "Notepad", "notes.txt")]
        with mock.patch.object(self.launcher, "top_level_windows", return_value=windows):
            app_windows = self.launcher.app_windows
            self.assertEqual([w[0] for w in app_windows({"window": "webview", "window_pid": 1})], [101])
            self.assertEqual([w[0] for w in app_windows({"window": "appmode", "window_pid": 1})], [102])
            self.assertEqual(app_windows({"window": "browser", "window_pid": None}), [])
        self.assertFalse(self.launcher.bring_to_front([]))

    def test_splash_and_window_helpers_need_no_network(self):
        for text in (self.launcher.SPLASH_HTML, self.launcher.WINDOW_HELPER_JS):
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
        self.assertIn("<title>ZehnMS</title>", self.launcher.SPLASH_HTML)
