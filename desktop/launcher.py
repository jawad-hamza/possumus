"""ZehnMS desktop launcher: one shop, one Windows PC, no Python install needed.

Double-clicking the shortcut runs this file (frozen by PyInstaller, see zehnms.spec). It

1. keeps every piece of shop data under %LOCALAPPDATA%\\ZehnMS (database, uploads, logs,
   backups, secret key) so upgrades and uninstalls never touch it,
2. applies database migrations (after taking a safety copy of the database),
3. serves the app with waitress, in this process, on http://127.0.0.1:8765 (or the next free
   port), and
4. opens ZehnMS in its own application window: no browser tabs, no address bar.

The window is opened with the first strategy that works on the PC:

    webview   pywebview on the Edge WebView2 runtime (the system web engine; nothing bundled)
    appmode   Microsoft Edge or Chrome in app mode (`--app=`), also a chromeless window
    browser   a tab in the default browser (last resort)

`--window <strategy>` (or ZEHNMS_WINDOW, or "window" in config.json) picks where that chain
starts; `--no-window` serves without opening anything. Closing the window stops the server.
Starting ZehnMS a second time brings the existing window to the front. `--stop` shuts a running
copy down. `--lan` also listens on the network so a second till can connect.
`--manage <command>` runs a Django management command against the shop's data.
"""

import argparse
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path


PROCESS_STARTED = time.perf_counter()

APP_NAME = "ZehnMS"
DEFAULT_PORT = 8765
LOCK_FILE_NAME = "zehnms.lock"
STOP_FILE_NAME = "zehnms.stop"
SECRET_KEY_FILE_NAME = "secret_key.txt"
CONFIG_FILE_NAME = "config.json"
ENV_FILE_NAME = "zehnms.env"
WINDOW_STATE_FILE_NAME = "window.json"
SELF_TEST_FILE_NAME = "selftest.json"
WEB_PROFILE_DIR_NAME = "webview-profile"
SAFETY_BACKUP_PREFIX = "auto-before-update-"
SAFETY_BACKUPS_TO_KEEP = 5

# One till keeps about six connections open while a page loads (the page plus its CSS, JS and
# fonts); a second till on the LAN does the same. Idle waitress threads only wait on a queue.
SERVER_THREADS = 8
SERVER_THREADS_LAN = 12

WINDOW_STRATEGIES = ("webview", "appmode", "browser")
WINDOW_NONE = "none"
DEFAULT_WINDOW_SIZE = (1366, 860)
MIN_WINDOW_SIZE = (1024, 640)
# On a screen with no room to spare around the default size (the usual 1366x768 shop laptop)
# the window starts maximised.
MAXIMISE_BELOW_SCREEN = (1500, 940)
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
APP_USER_MODEL_ID = "ZehnMS.Desktop"

IS_FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

# Shown the moment the window exists, while Django is still starting behind it. Inline on purpose:
# nothing here may need the server or the network.
SPLASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ZehnMS</title>
<style>
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;background:#f4f6f8;color:#1f2a37;
       font-family:"Segoe UI",system-ui,sans-serif}
  main{text-align:center}
  h1{font-size:34px;font-weight:600;letter-spacing:.5px;margin:0 0 18px}
  p{margin:0;color:#5b6775;font-size:15px}
  i{display:block;width:34px;height:34px;margin:0 auto 22px;border-radius:50%;
    border:3px solid #d5dbe2;border-top-color:#1f2a37;animation:turn .9s linear infinite}
  @keyframes turn{to{transform:rotate(360deg)}}
  @media (prefers-reduced-motion:reduce){i{animation:none}}
</style></head>
<body><main><i></i><h1>ZehnMS</h1><p>Opening your shop&hellip;</p></main></body></html>"""

# A chromeless window has no toolbar, so give the keyboard what the toolbar offered, and keep
# in-app links that ask for a new window inside this one (a browser tab would show a URL and
# would not be signed in).
WINDOW_HELPER_JS = """(function () {
  if (window.__zehnmsWindowHelper) { return; }
  window.__zehnmsWindowHelper = true;
  document.addEventListener('keydown', function (event) {
    var key = event.key;
    if (key === 'F5' || (event.ctrlKey && (key === 'r' || key === 'R'))) { event.preventDefault(); location.reload(); }
    else if (event.altKey && key === 'ArrowLeft') { event.preventDefault(); history.back(); }
    else if (event.altKey && key === 'ArrowRight') { event.preventDefault(); history.forward(); }
    else if (event.ctrlKey && (key === 'p' || key === 'P')) { event.preventDefault(); window.print(); }
  }, true);
  document.addEventListener('click', function (event) {
    var link = event.target && event.target.closest ? event.target.closest('a[target="_blank"]') : null;
    if (link && link.origin === location.origin) { link.removeAttribute('target'); }
  }, true);
})();"""


def default_data_dir():
    override = os.environ.get("ZEHNMS_DATA_DIR", "").strip()
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def redirect_output_when_windowed(data_dir):
    """The windowed exe has no console: sys.stdout/stderr are None and any print would be lost."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(log_dir / "launcher.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


def say(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def since_start():
    return round(time.perf_counter() - PROCESS_STARTED, 2)


def show_error(message):
    say(f"ERROR: {message}")
    if IS_FROZEN and os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        except Exception:
            pass


def load_config(data_dir):
    try:
        config = json.loads((data_dir / CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def load_or_create_secret_key(data_dir):
    key_file = data_dir / SECRET_KEY_FILE_NAME
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if len(key) < 50:
        key = secrets.token_urlsafe(64)
        key_file.write_text(key, encoding="utf-8")
    return key


def local_ipv4_addresses():
    addresses = set()
    try:
        addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    # Connecting a UDP socket sends nothing; it only asks the OS which interface faces the LAN.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(address for address in addresses if not address.startswith("127."))


def read_lock(data_dir):
    try:
        lock = json.loads((data_dir / LOCK_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return lock if isinstance(lock, dict) and lock.get("port") else None


def write_lock(data_dir, *, port, lan_urls, window="", window_pid=None):
    payload = {
        "pid": os.getpid(),
        "port": port,
        "url": f"http://127.0.0.1:{port}/",
        "lan_urls": lan_urls,
        # The kind of window that shows the app and the process that owns it: what a second
        # launch brings to the front instead of starting another server.
        "window": window,
        "window_pid": window_pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    (data_dir / LOCK_FILE_NAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def update_lock(data_dir, **changes):
    lock = read_lock(data_dir)
    if lock is None:
        return
    lock.update(changes)
    try:
        (data_dir / LOCK_FILE_NAME).write_text(json.dumps(lock, indent=2), encoding="utf-8")
    except OSError:
        pass


def remove_file(path):
    try:
        path.unlink()
    except OSError:
        pass


def is_healthy(port, timeout=2.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz/", timeout=timeout) as response:
            return response.status == 200 and json.loads(response.read().decode("utf-8")).get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def running_instance(data_dir):
    lock = read_lock(data_dir)
    if lock and is_healthy(lock["port"]):
        return lock
    return None


# --------------------------------------------------------------------------------------------
# Talking to the copy that is already running. Windows kernel objects, so nothing polls.
# --------------------------------------------------------------------------------------------

_KEPT_HANDLES = []  # handles that must live as long as the process


def instance_token(data_dir):
    """Shared by every launcher that works on the same data folder."""
    return hashlib.sha256(str(Path(data_dir).resolve()).lower().encode("utf-8")).hexdigest()[:16]


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    return kernel32


class StopSignal:
    """How `--stop` reaches the running copy.

    On Windows it is a named event: the running copy sleeps inside WaitForSingleObject and uses
    no CPU until the event is set. Elsewhere (a source checkout on Linux or macOS) it falls back
    to looking for a stop file once a second.
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.handle = None
        if os.name == "nt":
            try:
                self.handle = _kernel32().CreateEventW(None, True, False, self.event_name(data_dir)) or None
            except OSError:
                self.handle = None
        remove_file(data_dir / STOP_FILE_NAME)

    @staticmethod
    def event_name(data_dir):
        return f"Local\\{APP_NAME}-stop-{instance_token(data_dir)}"

    def wait(self):
        if self.handle:
            _kernel32().WaitForSingleObject(self.handle, 0xFFFFFFFF)
            return
        stop_file = self.data_dir / STOP_FILE_NAME
        while not stop_file.exists():
            time.sleep(1.0)
        remove_file(stop_file)

    @classmethod
    def request(cls, data_dir):
        """Ask the running copy to stop. True when the request was delivered."""
        if os.name != "nt":
            (data_dir / STOP_FILE_NAME).write_text("stop", encoding="utf-8")
            return True
        try:
            kernel32 = _kernel32()
            handle = kernel32.OpenEventW(0x0002, False, cls.event_name(data_dir))  # EVENT_MODIFY_STATE
            if not handle:
                return False
            kernel32.SetEvent(handle)
            kernel32.CloseHandle(handle)
            return True
        except OSError:
            return False


def claim_single_instance(data_dir):
    """False when another launcher already owns this data folder (it may still be starting)."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        handle = _kernel32().CreateMutexW(None, False, f"Local\\{APP_NAME}-instance-{instance_token(data_dir)}")
        already_owned = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    except OSError:
        return True
    if already_owned:
        _kernel32().CloseHandle(handle)
        return False
    _KEPT_HANDLES.append(handle)
    return True


def release_single_instance():
    while _KEPT_HANDLES:
        try:
            _kernel32().CloseHandle(_KEPT_HANDLES.pop())
        except OSError:
            pass


def wait_for_running_instance(data_dir, timeout=60.0):
    """Another launcher is still starting: wait for it, in this short-lived process only."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lock = running_instance(data_dir)
        if lock:
            return lock
        time.sleep(0.5)
    return None


def terminate_process(pid):
    try:
        os.kill(int(pid), 15)
    except (OSError, ValueError, TypeError):
        pass


def stop_running_instance(data_dir):
    lock = running_instance(data_dir)
    if lock is None:
        remove_file(data_dir / LOCK_FILE_NAME)
        say("ZehnMS is not running.")
        return 0
    StopSignal.request(data_dir)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not (data_dir / LOCK_FILE_NAME).exists():
            say("ZehnMS stopped.")
            return 0
        time.sleep(0.3)
    # The server did not react to the polite request; end the processes it recorded.
    terminate_process(lock.get("pid"))
    if lock.get("window_pid") and lock.get("window_pid") != lock.get("pid"):
        terminate_process(lock["window_pid"])
    remove_file(data_dir / LOCK_FILE_NAME)
    remove_file(data_dir / STOP_FILE_NAME)
    say("ZehnMS was stopped forcibly.")
    return 0


# --------------------------------------------------------------------------------------------
# Windows on screen: finding ours, bringing it forward, closing it
# --------------------------------------------------------------------------------------------

def top_level_windows(pid):
    """Visible, titled, unowned top-level windows of a process, as (handle, class name, title)."""
    if os.name != "nt" or not pid:
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(handle, _):
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner_pid))
        if owner_pid.value != int(pid) or not user32.IsWindowVisible(handle) or user32.GetWindow(handle, 4):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(handle, title, 512)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(handle, class_name, 256)
        if title.value:
            found.append((handle, class_name.value, title.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def app_windows(lock):
    """The ZehnMS window(s) a lock file points at; the filters guard against a recycled process id."""
    windows = top_level_windows(lock.get("window_pid"))
    if lock.get("window") == "webview":
        return [window for window in windows if window[2] == APP_NAME]
    if lock.get("window") == "appmode":
        return [window for window in windows if window[1].startswith("Chrome_WidgetWin")]
    return []


def bring_to_front(windows):
    if not windows:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    handle = windows[0][0]
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, 9)  # SW_RESTORE
    user32.SetForegroundWindow(handle)
    return True


def close_windows(windows):
    import ctypes

    for handle, _, _ in windows:
        ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE


def set_taskbar_identity():
    """Give the window its own taskbar button instead of grouping under python.exe."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (OSError, AttributeError):
        pass


def icon_path():
    path = PROJECT_ROOT / "desktop" / "zehnms.ico"
    return str(path) if path.is_file() else None


# --------------------------------------------------------------------------------------------
# Window strategy and geometry
# --------------------------------------------------------------------------------------------

def window_strategy_chain(requested):
    """Strategies to try, in order. A forced strategy is where the chain starts: a shop is
    better served by the next kind of window than by an error message."""
    requested = (requested or "auto").strip().lower()
    if requested == WINDOW_NONE:
        return []
    if requested in WINDOW_STRATEGIES:
        return list(WINDOW_STRATEGIES[WINDOW_STRATEGIES.index(requested):])
    if requested != "auto":
        say(f"Unknown window strategy {requested!r}; choosing automatically.")
    return list(WINDOW_STRATEGIES)


def requested_window_strategy(arguments, config):
    if arguments.no_browser or config.get("open_browser") is False:
        return WINDOW_NONE
    return arguments.window or os.environ.get("ZEHNMS_WINDOW", "").strip() or str(config.get("window") or "") or "auto"


def webview2_runtime_version():
    """Version of the Evergreen WebView2 runtime, or "" when it is not installed.

    These are the registry locations Microsoft documents for detecting the runtime. Without it
    pywebview would quietly fall back to the Internet Explorer engine, which cannot render the app.
    """
    if os.name != "nt":
        return ""
    import winreg

    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"),
    )
    for hive, key_path in locations:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return version
    return ""


def find_app_mode_browser():
    """Path of Microsoft Edge (preferred: it ships with Windows) or Chrome, else None."""
    if os.name != "nt":
        return None
    import winreg

    layouts = (("msedge.exe", r"Microsoft\Edge\Application"), ("chrome.exe", r"Google\Chrome\Application"))
    for executable, folder in layouts:
        candidates = []
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                candidates.append(
                    winreg.QueryValue(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}")
                )
            except OSError:
                pass
        for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(variable, "").strip()
            if base:
                candidates.append(str(Path(base) / folder / executable))
        for candidate in candidates:
            candidate = (candidate or "").strip().strip('"')
            if candidate and Path(candidate).is_file():
                return candidate
    return None


def screen_metrics():
    """(primary width, primary height, virtual left, top, width, height) in 96-dpi pixels, or None."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            scale = (user32.GetDpiForSystem() or 96) / 96
        except AttributeError:
            scale = 1.0
        values = [user32.GetSystemMetrics(index) for index in (0, 1, 76, 77, 78, 79)]
    except OSError:
        return None
    if values[0] <= 0 or values[1] <= 0:
        return None
    return tuple(int(value / scale) for value in values)


def load_window_state(data_dir):
    try:
        state = json.loads((data_dir / WINDOW_STATE_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_window_state(data_dir, state):
    try:
        (data_dir / WINDOW_STATE_FILE_NAME).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def plan_window(saved, screen):
    """Size, position and maximised flag for the window, from last run's state and the screen.

    x and y are None when the window should simply be centred.
    """
    def whole_number(value):
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    width = whole_number(saved.get("width")) or DEFAULT_WINDOW_SIZE[0]
    height = whole_number(saved.get("height")) or DEFAULT_WINDOW_SIZE[1]
    x, y = whole_number(saved.get("x")), whole_number(saved.get("y"))
    maximized = saved.get("maximized") if isinstance(saved.get("maximized"), bool) else None

    if screen:
        screen_width, screen_height, virtual_left, virtual_top, virtual_width, virtual_height = screen
        if maximized is None:
            maximized = screen_width < MAXIMISE_BELOW_SCREEN[0] or screen_height < MAXIMISE_BELOW_SCREEN[1]
        width = min(width, max(screen_width, MIN_WINDOW_SIZE[0]))
        height = min(height, max(screen_height, MIN_WINDOW_SIZE[1]))
        if x is not None and y is not None:
            # A monitor may have been unplugged since: keep the position only if the title bar is reachable.
            reachable = (
                virtual_left - width + 120 <= x <= virtual_left + virtual_width - 120
                and virtual_top - 10 <= y <= virtual_top + virtual_height - 80
            )
            if not reachable:
                x = y = None
    width, height = max(width, MIN_WINDOW_SIZE[0]), max(height, MIN_WINDOW_SIZE[1])
    if x is None or y is None:
        x = y = None
    return {"width": width, "height": height, "x": x, "y": y, "maximized": bool(maximized)}


# --------------------------------------------------------------------------------------------
# Django and the in-process server
# --------------------------------------------------------------------------------------------

def configure_environment(data_dir, *, allowed_hosts):
    for folder in ("media", "logs", "backups"):
        (data_dir / folder).mkdir(parents=True, exist_ok=True)
    os.environ["DEPLOYMENT_MODE"] = "desktop"
    os.environ["DJANGO_DEBUG"] = "false"
    os.environ["ZEHNMS_DATA_DIR"] = str(data_dir)
    os.environ["DJANGO_SECRET_KEY"] = load_or_create_secret_key(data_dir)
    os.environ["DJANGO_ALLOWED_HOSTS"] = ",".join(allowed_hosts)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "possumus.settings")
    # Optional shop-specific overrides (SMTP for emailed reports, ...) live next to the data.
    env_file = data_dir / ENV_FILE_NAME
    if env_file.is_file():
        os.environ["ZEHNMS_ENV_FILE"] = str(env_file)
    # A shop PC has no DATABASE_URL; make sure a stray machine-wide one cannot redirect the data.
    for name in ("DATABASE_URL", "DB_ENGINE", "SQLITE_PATH", "VERCEL"):
        os.environ.pop(name, None)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def take_safety_backup(database_path, backup_dir):
    """Copy the database before an update changes its structure."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{SAFETY_BACKUP_PREFIX}{stamp}.sqlite3"
    source = sqlite3.connect(str(database_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    for stale in sorted(backup_dir.glob(f"{SAFETY_BACKUP_PREFIX}*.sqlite3"))[:-SAFETY_BACKUPS_TO_KEEP]:
        remove_file(stale)
    return target


def prepare_database():
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    database_path = Path(settings.DATABASES["default"]["NAME"])
    is_existing_shop = database_path.is_file() and database_path.stat().st_size > 0

    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if not pending:
        connection.close()
        return
    if is_existing_shop:
        connection.close()
        backup = take_safety_backup(database_path, Path(settings.BACKUP_DIR))
        say(f"Database copied to {backup.name} before updating.")
    say(f"Applying {len(pending)} database update(s)...")
    call_command("migrate", interactive=False, verbosity=0)
    connection.close()


def ensure_static_files():
    """The frozen build ships collected static files; a source checkout collects them on first run."""
    from django.conf import settings
    from django.core.management import call_command

    if (Path(settings.STATIC_ROOT) / "staticfiles.json").is_file():
        return
    if IS_FROZEN:
        raise RuntimeError("The installation is incomplete (static files are missing). Reinstall ZehnMS.")
    say("Collecting static files...")
    call_command("collectstatic", interactive=False, verbosity=0)


def server_thread_count(lan_mode, configured=None):
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = 0
    if configured:
        return max(4, min(configured, 32))
    return SERVER_THREADS_LAN if lan_mode else SERVER_THREADS


def create_server(application, *, host, preferred_port, threads=SERVER_THREADS):
    from waitress.server import create_server as waitress_create_server

    options = {"host": host, "threads": threads, "ident": APP_NAME, "clear_untrusted_proxy_headers": True}
    try:
        return waitress_create_server(application, port=preferred_port, **options)
    except OSError:
        say(f"Port {preferred_port} is busy; choosing a free one.")
        return waitress_create_server(application, port=0, **options)


class ShopServer:
    """Django plus waitress inside this process, on loopback unless LAN mode is on.

    `boot()` may be called from any thread and does its work once. The window strategies decide
    which thread serves: the webview needs the main thread for its window, so the server gets a
    background thread; otherwise the server loop simply runs on the main thread.
    """

    def __init__(self, data_dir, *, lan_mode, preferred_port, extra_allowed_hosts=(), threads=None):
        self.data_dir = data_dir
        self.lan_mode = lan_mode
        self.preferred_port = preferred_port
        self.extra_allowed_hosts = list(extra_allowed_hosts)
        self.threads = server_thread_count(lan_mode, threads)
        self.server = None
        self.url = ""
        self.lan_urls = []
        self.ready_after = None
        self._boot_lock = threading.Lock()
        self._booted = False
        self._boot_error = None
        self._thread = None
        self._shutdown_requested = False
        self._window = {"window": "", "window_pid": None}
        self._lock_file_guard = threading.Lock()

    def set_window(self, strategy, window_pid):
        """Record which window shows the app; the lock file may not exist yet (Django still starting)."""
        with self._lock_file_guard:
            self._window = {"window": strategy, "window_pid": window_pid}
            update_lock(self.data_dir, **self._window)

    def boot(self):
        """Prepare Django and bind the port. Returns the local URL; raises what went wrong."""
        with self._boot_lock:
            if not self._booted:
                try:
                    self._boot()
                except BaseException as exc:
                    self._boot_error = exc
                finally:
                    self._booted = True
            if self._boot_error is not None:
                raise self._boot_error
            return self.url

    def _boot(self):
        allowed_hosts = ["localhost", "127.0.0.1"]
        lan_addresses = local_ipv4_addresses() if self.lan_mode else []
        if self.lan_mode:
            allowed_hosts += lan_addresses + [socket.gethostname()] + self.extra_allowed_hosts
        configure_environment(self.data_dir, allowed_hosts=allowed_hosts)

        import django

        django.setup()
        prepare_database()
        ensure_static_files()

        from django.core.wsgi import get_wsgi_application

        self.server = create_server(
            get_wsgi_application(),
            host="0.0.0.0" if self.lan_mode else "127.0.0.1",
            preferred_port=self.preferred_port,
            threads=self.threads,
        )
        port = int(self.server.effective_port)
        self.url = f"http://127.0.0.1:{port}/"
        self.lan_urls = [f"http://{address}:{port}/" for address in lan_addresses]
        # The socket is already listening: a window opened now simply waits for the first answer.
        with self._lock_file_guard:
            write_lock(self.data_dir, port=port, lan_urls=self.lan_urls, **self._window)
        self.ready_after = since_start()
        say(f"{APP_NAME} is running at {self.url}  (data: {self.data_dir}; ready after {self.ready_after} s)")
        for url in self.lan_urls:
            say(f"Other tills on this network can open {url}")

    def start_in_background(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._boot_and_serve, name="zehnms-server", daemon=True)
            self._thread.start()

    def _boot_and_serve(self):
        try:
            self.boot()
        except BaseException:
            return  # whoever waits on boot() gets the error
        self._serve()

    def _serve(self):
        if self._shutdown_requested:
            return
        try:
            self.server.run()
        except Exception:
            import traceback

            traceback.print_exc()

    def serve_until_shutdown(self):
        """Blocks until request_shutdown() (or Ctrl+C when the loop runs on the main thread)."""
        if self._thread is not None:
            self._thread.join()
        else:
            self.boot()
            self._serve()

    def request_shutdown(self):
        """Ends the server loop from any thread, without polling: the closing runs inside the loop."""
        self._shutdown_requested = True
        server = self.server
        if server is None:
            return

        def close_everything():
            from waitress import wasyncore

            wasyncore.close_all(server._map)

        try:
            server.trigger.pull_trigger(close_everything)
        except OSError:
            pass

    def finish(self):
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.server is not None:
            try:
                self.server.task_dispatcher.shutdown(timeout=5)
            except Exception:
                pass
        try:
            from django.db import connections

            connections.close_all()
        except Exception:
            pass
        remove_file(self.data_dir / LOCK_FILE_NAME)
        release_single_instance()  # from here on a new launch may start its own server
        say(f"{APP_NAME} stopped.")


# --------------------------------------------------------------------------------------------
# The three kinds of window
# --------------------------------------------------------------------------------------------

class WindowSession:
    """What a window strategy needs to know, whether this process owns the server or only shows it."""

    def __init__(self, data_dir, *, get_url, debug=False, self_test=False):
        self.data_dir = data_dir
        self.get_url = get_url  # blocks until the server answers; raises if it could not start
        self.debug = debug
        self.self_test = self_test
        self.strategy = ""
        self.closers = []  # callables that close whatever window is open
        self.on_window_gone = None  # set by the owner: what to do when the shop closes the window
        self.on_window_opened = None  # set by the owner: records the window in the lock file
        self.report = {}

    def announce(self, strategy, window_pid):
        self.strategy = strategy
        if self.on_window_opened:
            self.on_window_opened(strategy, window_pid)
        else:
            update_lock(self.data_dir, window=strategy, window_pid=window_pid)

    def close_window(self):
        for close in list(self.closers):
            try:
                close()
            except Exception:
                pass

    def write_self_test(self, **details):
        self.report.update(details)
        self.report.update(strategy=self.strategy, finished_after_s=since_start())
        try:
            (self.data_dir / SELF_TEST_FILE_NAME).write_text(json.dumps(self.report, indent=2), encoding="utf-8")
        except OSError:
            pass


SELF_TEST_PROBE_JS = """(function () {
  var navigation = performance.getEntriesByType('navigation')[0] || {};
  var outside = performance.getEntriesByType('resource')
    .map(function (entry) { return entry.name; })
    .filter(function (name) { return name.indexOf(location.origin) !== 0 && name.indexOf('data:') !== 0; });
  return {
    href: location.href,
    title: document.title,
    has_form: !!document.querySelector('form'),
    stylesheets: document.styleSheets.length,
    resources: performance.getEntriesByType('resource').length,
    resources_outside_this_pc: outside,
    print_type: typeof window.print,
    page_load_ms: Math.round(navigation.loadEventEnd || navigation.duration || 0)
  };
})()"""


def open_webview_window(session):
    """pywebview on WebView2. False means this PC cannot do it and the next strategy should try."""
    runtime = webview2_runtime_version()
    if not runtime:
        say("Window: the WebView2 runtime is not installed.")
        return False
    try:
        import webview
        from webview.platforms import winforms as backend  # starts .NET: fails here, early, if it cannot
    except Exception as exc:
        say(f"Window: pywebview is not usable here ({exc!r}).")
        return False
    if getattr(backend, "renderer", "") != "edgechromium":
        say(f"Window: pywebview would use {getattr(backend, 'renderer', '?')!r}, not WebView2.")
        return False

    data_dir = session.data_dir
    plan = plan_window(load_window_state(data_dir), screen_metrics())
    profile_dir = data_dir / WEB_PROFILE_DIR_NAME / "webview2"
    profile_dir.mkdir(parents=True, exist_ok=True)

    webview.settings["ALLOW_DOWNLOADS"] = True  # CSV and PDF exports go through a Save As dialog
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False  # --debug allows F12; it does not force it open
    window = webview.create_window(
        APP_NAME,
        html=SPLASH_HTML,
        width=plan["width"],
        height=plan["height"],
        x=plan["x"],
        y=plan["y"],
        min_size=MIN_WINDOW_SIZE,
        maximized=plan["maximized"],
        background_color="#f4f6f8",
        text_select=True,
        zoomable=True,
    )
    outcome = {"maximized": plan["maximized"], "unusable": False, "error": None, "shown": False}

    def note_shown():
        outcome["shown"] = True
        session.report["window_shown_after_s"] = since_start()
        say(f"Window shown after {since_start()} s (WebView2 {runtime}).")

    def remember_geometry():
        # Runs on the window's own thread just before it closes.
        saved = load_window_state(data_dir)
        saved["maximized"] = bool(outcome["maximized"])
        if not outcome["maximized"]:
            try:
                x, y, width, height = window.x, window.y, window.width, window.height
            except Exception:
                x = None
            if x is not None and x > -30000 and width >= MIN_WINDOW_SIZE[0] and height >= MIN_WINDOW_SIZE[1]:
                saved.update(x=x, y=y, width=width, height=height)
        save_window_state(data_dir, saved)

    def add_window_helpers():
        try:
            window.evaluate_js(WINDOW_HELPER_JS)
        except Exception:
            pass

    window.events.shown += note_shown
    window.events.maximized += lambda: outcome.update(maximized=True)
    window.events.restored += lambda: outcome.update(maximized=False)
    window.events.closing += remember_geometry
    window.events.loaded += add_window_helpers
    session.closers.append(window.destroy)

    def pilot():
        # The splash page rendering proves WebView2 really started.
        if not window.events.loaded.wait(45):
            outcome["unusable"] = True
            window.destroy()
            return
        try:
            url = session.get_url()
        except BaseException as exc:
            outcome["error"] = exc
            window.destroy()
            return
        window.events.loaded.clear()
        window.load_url(url)
        loaded = window.events.loaded.wait(90)
        session.report["first_page_after_s"] = since_start()
        say(f"First page {'shown' if loaded else 'NOT shown'} after {since_start()} s.")
        if session.self_test:
            try:
                probe = window.evaluate_js(SELF_TEST_PROBE_JS)
            except Exception as exc:
                probe = {"error": repr(exc)}
            session.write_self_test(
                renderer=backend.renderer,
                webview2_runtime=runtime,
                window_titles=[title for _, _, title in top_level_windows(os.getpid())],
                page=probe,
                loaded=bool(loaded),
            )
            window.destroy()

    session.announce("webview", os.getpid())
    try:
        webview.start(
            pilot,
            debug=session.debug,
            private_mode=False,  # keep the sign-in cookie and the HTTP cache between runs
            storage_path=str(profile_dir),
            icon=icon_path(),
        )
    except Exception as exc:
        say(f"Window: pywebview failed ({exc!r}).")
        outcome["unusable"] = not outcome["shown"]
        if outcome["shown"]:
            raise
    session.closers.remove(window.destroy)
    if outcome["error"] is not None:
        raise outcome["error"]
    if outcome["unusable"]:
        say("Window: WebView2 did not start.")
        return False
    return True


def open_app_mode_window(session):
    """Edge or Chrome with --app: a chromeless window run by the installed browser."""
    browser = find_app_mode_browser()
    if not browser:
        say("Window: neither Microsoft Edge nor Chrome was found.")
        return False
    url = session.get_url()
    data_dir = session.data_dir
    profile_dir = data_dir / WEB_PROFILE_DIR_NAME / Path(browser).stem
    profile_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_window(load_window_state(data_dir), screen_metrics())
    command = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={plan['width']},{plan['height']}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",  # the process must end when the window closes
        "--disable-features=msEdgeSidebarV2,Translate",
    ]
    if plan["maximized"]:
        command.append("--start-maximized")
    try:
        process = subprocess.Popen(command, close_fds=True)
    except OSError as exc:
        say(f"Window: could not start {Path(browser).name} ({exc}).")
        return False
    started = time.monotonic()
    session.announce("appmode", process.pid)
    say(f"Window: {Path(browser).name} app mode (pid {process.pid}) after {since_start()} s.")

    def close():
        close_windows(top_level_windows(process.pid))
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()

    def watch():
        if session.self_test:
            time.sleep(8)
            session.write_self_test(
                browser=browser,
                window_titles=[title for _, _, title in top_level_windows(process.pid)],
                browser_running=process.poll() is None,
            )
            close()
        process.wait()  # sleeps in the OS until the shop closes the window
        if time.monotonic() - started < 3 and not session.self_test:
            # The browser handed the window to another of its processes; we cannot tell when it closes.
            say("Window: the browser process ended at once; ZehnMS keeps running until it is stopped.")
            return
        if session.on_window_gone:
            session.on_window_gone()

    session.closers.append(close)
    threading.Thread(target=watch, name="zehnms-window", daemon=True).start()
    return True


def open_browser_tab(session):
    url = session.get_url()
    session.announce("browser", None)
    say("Window: opening the default browser.")
    webbrowser.open(url)
    if session.self_test:
        session.write_self_test()
        if session.on_window_gone:
            session.on_window_gone()
    return True


WINDOW_OPENERS = {"webview": open_webview_window, "appmode": open_app_mode_window, "browser": open_browser_tab}


def open_window(session, chain):
    """Try each strategy in turn. Returns the one that worked ("" if none).

    The webview strategy returns only once its window has been closed; the other two return as
    soon as the window is open.
    """
    for strategy in chain:
        if WINDOW_OPENERS[strategy](session):
            return strategy
    return ""


def show_existing_instance(data_dir, existing, arguments, config):
    """ZehnMS is already running on this PC: show it instead of starting a second server."""
    say(f"ZehnMS is already running at {existing['url']}")
    chain = window_strategy_chain(requested_window_strategy(arguments, config))
    if not chain:
        return 0
    if bring_to_front(app_windows(existing)):
        return 0
    # It runs without a window (started with --no-window, or kept running after its window closed).
    if existing.get("window") == "browser":
        webbrowser.open(existing["url"])
        return 0
    set_taskbar_identity()
    session = WindowSession(data_dir, get_url=lambda: existing["url"], debug=arguments.debug)
    open_window(session, chain)
    return 0


def run_management_command(data_dir, command_arguments):
    """Support tool for the installed copy, which has no manage.py (use zehnms-cli.exe to see output)."""
    from django.core.management import execute_from_command_line

    configure_environment(data_dir, allowed_hosts=["localhost", "127.0.0.1"])
    execute_from_command_line([APP_NAME, *(command_arguments or ["help"])])
    return 0


def parse_arguments(argv):
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Start the ZehnMS point of sale on this PC.")
    parser.add_argument("--port", type=int, help=f"Preferred port (default {DEFAULT_PORT}).")
    parser.add_argument("--lan", action="store_true", help="Also accept connections from other PCs on the network.")
    parser.add_argument("--allow-host", action="append", default=[], help="Extra host name or IP other tills use.")
    parser.add_argument(
        "--window",
        choices=["auto", *WINDOW_STRATEGIES, WINDOW_NONE],
        help="How to show the app: webview (WebView2 window), appmode (Edge/Chrome app window), "
        "browser (default browser) or none. Default: the first that works. Also: ZEHNMS_WINDOW.",
    )
    parser.add_argument(
        "--no-window", "--no-browser", dest="no_browser", action="store_true", help="Serve only; open nothing."
    )
    parser.add_argument("--debug", action="store_true", help="Keep the right-click menu and developer tools (F12).")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", help="Folder for the database, uploads, logs and backups.")
    parser.add_argument("--stop", action="store_true", help="Stop the running copy and exit.")
    parser.add_argument("--status", action="store_true", help="Report whether ZehnMS is running and exit.")
    parser.add_argument(
        "--manage",
        nargs=argparse.REMAINDER,
        metavar="COMMAND",
        help="Run a maintenance command against this PC's data, e.g. --manage reset_owner_password.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    if arguments.data_dir:
        os.environ["ZEHNMS_DATA_DIR"] = arguments.data_dir
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    data_dir = data_dir.resolve()  # the web engines are handed this path and need it absolute
    os.environ["ZEHNMS_DATA_DIR"] = str(data_dir)
    redirect_output_when_windowed(data_dir)

    if arguments.stop:
        return stop_running_instance(data_dir)

    if arguments.manage is not None:
        return run_management_command(data_dir, arguments.manage)

    config = load_config(data_dir)
    existing = running_instance(data_dir)
    if arguments.status:
        if existing:
            say(f"Running at {existing['url']} (window: {existing.get('window') or 'none'})")
            for url in existing.get("lan_urls") or []:
                say(f"Other tills: {url}")
        else:
            say("Not running.")
        return 0 if existing else 1
    if existing is None and not claim_single_instance(data_dir):
        # A double double-click: the first copy is still starting. Wait for it instead of racing it.
        existing = wait_for_running_instance(data_dir)
        if existing is None:
            show_error(f"{APP_NAME} is already starting but has not answered yet. Try again in a moment.")
            return 1
    if existing:
        return show_existing_instance(data_dir, existing, arguments, config)

    lan_mode = bool(arguments.lan or config.get("lan"))
    chain = window_strategy_chain(requested_window_strategy(arguments, config))
    keep_running = bool(config.get("keep_running_after_close")) and not arguments.self_test

    stop_signal = StopSignal(data_dir)
    shop = ShopServer(
        data_dir,
        lan_mode=lan_mode,
        preferred_port=int(arguments.port or config.get("port") or DEFAULT_PORT),
        extra_allowed_hosts=[str(host) for host in config.get("allowed_hosts", [])] + arguments.allow_host,
        threads=config.get("threads"),
    )
    session = WindowSession(data_dir, get_url=shop.boot, debug=arguments.debug, self_test=arguments.self_test)
    session.on_window_opened = shop.set_window
    if not keep_running:
        session.on_window_gone = shop.request_shutdown

    def stop_when_asked():
        stop_signal.wait()
        say("Stop requested.")
        shop.request_shutdown()
        session.close_window()

    threading.Thread(target=stop_when_asked, name="zehnms-stop", daemon=True).start()

    exit_code = 0
    try:
        if chain:
            set_taskbar_identity()
        if chain and chain[0] == "webview":
            # The window needs the main thread, so Django starts on another one at the same moment.
            shop.start_in_background()
        else:
            shop.boot()
        strategy = open_window(session, chain)
        if chain and not strategy:
            say("No window could be opened; serving without one.")
        if strategy == "webview":
            # open_window returned, so the shop closed the window.
            if keep_running:
                say("Window closed; still serving until ZehnMS is stopped.")
                shop.set_window("", None)
            else:
                shop.request_shutdown()
        shop.serve_until_shutdown()
    except KeyboardInterrupt:
        pass
    except BaseException as exc:
        if isinstance(exc, SystemExit):
            raise
        import traceback

        traceback.print_exc()
        session.close_window()
        shop.request_shutdown()
        show_error(f"{APP_NAME} could not start.\n\n{exc}\n\nDetails: {data_dir / 'logs'}")
        exit_code = 1
    finally:
        session.close_window()
        shop.request_shutdown()
        shop.finish()
    return exit_code


def leave(exit_code):
    """End the process. With the WebView2 window loaded, skip Python's own teardown of the .NET
    runtime: it is slow, it does nothing for us (the server is closed, the lock file is gone, the
    logs are flushed), and the window has already disappeared."""
    if "clr" in sys.modules:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        os._exit(exit_code or 0)
    sys.exit(exit_code)


if __name__ == "__main__":
    leave(main())
