"""Screenshot signed-in ZehnMS pages at counter-PC and phone sizes. Dev tool; the app never imports it.

    .venv/Scripts/python.exe .impeccable/tools/shoot.py /dashboard/ /products/
    .venv/Scripts/python.exe .impeccable/tools/shoot.py --anon /login/
    .venv/Scripts/python.exe .impeccable/tools/shoot.py --full --only phone /settings/
    .venv/Scripts/python.exe .impeccable/tools/shoot.py --name till-open /pos/
    .venv/Scripts/python.exe .impeccable/tools/shoot.py --anon --full __dev__/kitchen-sink.html
    .venv/Scripts/python.exe .impeccable/tools/shoot.py --name goto-open --js "ZehnShell.openGoto()" dashboard/

Paths may be written without the leading slash (dashboard/). Git Bash rewrites a leading slash into
C:/Program Files/Git/...; that form is recognised and undone, so either spelling works.

How it works: db.sqlite3 is COPIED to a temp folder and Django is pointed at the copy, so the real
database is never written. A throwaway superuser (or --as USER) is signed in through Django's test
Client. A local server on one port answers every browser request by passing it through that signed-in
Client and serves /static/ from the project's static folders, so fonts, scripts and the page's own
fetch() calls all work. Headless Edge then writes the PNGs.

Output: .impeccable/shots/<name>-desktop.png (1366x768) and <name>-phone.png (390x844), plus the
rendered HTML next to them as <name>.html.
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
SIZES = {"desktop": (1366, 768), "phone": (390, 844)}
FRAME_URL = "/__frame__/"
DEV_URL = "/__dev__/"
MIN_WINDOW = 520
BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser(explicit=None):
    candidates = [explicit] if explicit else []
    candidates += list(BROWSERS)
    candidates += [shutil.which(name) for name in ("msedge", "chrome", "chromium", "google-chrome")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    raise SystemExit("No Edge or Chrome found. Pass --browser PATH.")


def boot_django(workdir: Path):
    """Point Django at a private copy of the database, then set it up."""
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "possumus.settings")
    os.environ.setdefault("DJANGO_DEBUG", "1")

    db_copy = workdir / "shoot.sqlite3"
    real_db = ROOT / "db.sqlite3"
    if real_db.exists():
        shutil.copy2(real_db, db_copy)

    from django.conf import settings

    default = dict(settings.DATABASES["default"])
    default.update({"ENGINE": "django.db.backends.sqlite3", "NAME": str(db_copy)})
    for key in ("USER", "PASSWORD", "HOST", "PORT"):
        default[key] = ""
    default["OPTIONS"] = {}
    settings.DATABASES["default"] = default
    settings.ALLOWED_HOSTS = ["*"]
    settings.MEDIA_ROOT = str(workdir / "media") if not Path(settings.MEDIA_ROOT).exists() else settings.MEDIA_ROOT

    import django

    django.setup()

    from django.core.management import call_command

    call_command("migrate", interactive=False, verbosity=0)
    return settings


def sign_in(username=None):
    from django.contrib.auth import get_user_model
    from django.test import Client

    from pos.models import CompanyProfile

    User = get_user_model()
    if username:
        user = User.objects.filter(username=username).first()
        if user is None:
            raise SystemExit(f"No user named {username!r} in the database copy.")
    else:
        user, _ = User.objects.get_or_create(
            username="shoot_admin",
            defaults={"is_superuser": True, "is_staff": True, "is_active": True, "first_name": "Shoot", "last_name": "Admin"},
        )
        user.is_superuser = user.is_staff = user.is_active = True
        user.set_unusable_password()
        user.save()
    if not CompanyProfile.objects.filter(is_active=True).exists():
        CompanyProfile.objects.create(name="Screenshot Traders", is_active=True)

    client = Client()
    client.force_login(user)
    return client


class Bridge:
    """Answers browser requests: static files from disk, everything else through the test Client."""

    def __init__(self, client, settings):
        self.client = client
        self.settings = settings
        self.lock = threading.Lock()
        self.inject = {}  # {path: javascript run on load}, set by --js
        self.static_url = "/" + settings.STATIC_URL.strip("/") + "/"
        self.media_url = "/" + settings.MEDIA_URL.strip("/") + "/"

    def static_file(self, relative):
        from django.contrib.staticfiles import finders

        relative = relative.split("?", 1)[0]
        found = finders.find(relative)
        if found:
            return Path(found)
        collected = Path(self.settings.STATIC_ROOT or "") / relative
        if collected.is_file():
            return collected
        # a hashed name (app.3f2a1b9c.css) falls back to the source file
        plain = re.sub(r"\.[0-9a-f]{8,12}(\.[^./]+)$", r"\1", relative)
        found = finders.find(plain) if plain != relative else None
        return Path(found) if found else None

    def fetch(self, method, path, body=b"", content_type=""):
        if path.startswith(FRAME_URL):
            # Headless Edge will not lay a window out narrower than about 500px, so a phone is an iframe.
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(path).query)
            width, height = int(query["w"][0]), int(query["h"][0])
            inner = query["u"][0].replace("&", "&amp;").replace('"', "&quot;")
            page = (
                "<!doctype html><meta charset=utf-8><title>frame</title>"
                "<style>html,body{margin:0;padding:0;overflow:hidden;background:#fff}iframe{display:block;border:0}</style>"
                f'<iframe src="{inner}" width="{width}" height="{height}"></iframe>'
            )
            return 200, {"Content-Type": "text/html; charset=utf-8"}, page.encode()
        if path.startswith(DEV_URL):
            # dev-only pages that live beside this tool, such as kitchen-sink.html
            here = Path(__file__).resolve().parent
            target = (here / path[len(DEV_URL):].split("?", 1)[0]).resolve()
            if target.is_file() and target.parent == here:
                kind = mimetypes.guess_type(str(target))[0] or "text/html"
                return 200, {"Content-Type": kind + "; charset=utf-8"}, target.read_bytes()
            return 404, {"Content-Type": "text/plain"}, b"no such dev page"
        if path.startswith(self.static_url):
            target = self.static_file(path[len(self.static_url):])
            if target is None:
                return 404, {"Content-Type": "text/plain"}, b"missing static file"
            kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if target.suffix == ".woff2":
                kind = "font/woff2"
            return 200, {"Content-Type": kind, "Cache-Control": "no-store"}, target.read_bytes()
        if path.startswith(self.media_url):
            target = Path(self.settings.MEDIA_ROOT) / path[len(self.media_url):].split("?", 1)[0]
            if target.is_file():
                kind = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                return 200, {"Content-Type": kind}, target.read_bytes()
            return 404, {"Content-Type": "text/plain"}, b"missing media file"

        with self.lock:
            if method == "POST":
                response = self.client.post(path, data=body, content_type=content_type or "application/octet-stream",
                                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            else:
                response = self.client.get(path)
            payload = b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content
        headers = {"Content-Type": response.get("Content-Type", "text/html; charset=utf-8"), "Cache-Control": "no-store"}
        script = self.inject.get(path)
        if script and "text/html" in headers["Content-Type"]:
            tag = "<script>window.addEventListener('load',function(){" + script + "});</script>"
            payload = payload.replace(b"</body>", tag.encode() + b"</body>", 1)
        if response.get("Location"):
            headers["Location"] = response["Location"]
        return response.status_code, headers, payload


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                status, headers, payload = bridge.fetch(method, self.path, body, self.headers.get("Content-Type", ""))
            except Exception as error:  # a page that raises still produces a picture of the failure
                status, headers, payload = 500, {"Content-Type": "text/plain; charset=utf-8"}, repr(error).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(payload)

        def do_GET(self):
            self._answer("GET")

        def do_HEAD(self):
            self._answer("HEAD")

        def do_POST(self):
            self._answer("POST")

        def log_message(self, *args):
            pass

    return Handler


def clean_path(raw):
    """Accept /dashboard/, dashboard/ and the C:/Program Files/Git/dashboard/ that Git Bash makes of the first."""
    from django.urls import Resolver404, resolve

    raw = raw.replace("\\", "/")
    path, _, query = raw.partition("?")
    marker = DEV_URL.strip("/")
    if marker in path:
        return DEV_URL + path.split(marker, 1)[1].lstrip("/")
    if re.match(r"^[A-Za-z]:/", path):
        parts = [part for part in path.split("/") if part]
        tail = "/" if path.endswith("/") else ""
        for start in range(1, len(parts) + 1):
            candidate = "/" + "/".join(parts[start:]) + (tail if parts[start:] else "")
            candidate = candidate.replace("//", "/")
            try:
                resolve(candidate)
            except Resolver404:
                continue
            path = candidate
            break
    if not path.startswith("/"):
        path = "/" + path
    return path + ("?" + query if query else "")


def slug_for(path):
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", path.split("?", 1)[0]).strip("-").lower()
    return slug or "home"


def capture(browser, url, target: Path, width, height, wait_ms, scale, profile: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-features=Translate,msEdgeSidebarV2",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        f"--force-device-scale-factor={scale}",
        f"--virtual-time-budget={wait_ms}",
        "--run-all-compositor-stages-before-draw",
        f"--screenshot={target}",
        url,
    ]
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
    return target.exists()


def main():
    parser = argparse.ArgumentParser(description="Screenshot ZehnMS pages at 1366x768 and 390x844.")
    parser.add_argument("paths", nargs="+", help="URL paths, such as /dashboard/ or /products/?q=tea")
    parser.add_argument("--anon", action="store_true", help="do not sign in (login, signup, reset pages)")
    parser.add_argument("--as", dest="username", help="sign in as this existing user instead of a throwaway superuser")
    parser.add_argument("--only", choices=sorted(SIZES), help="capture one size only")
    parser.add_argument("--full", action="store_true", help="tall window so the whole page shows (see --height)")
    parser.add_argument("--height", type=int, default=2400, help="window height used with --full (default 2400)")
    parser.add_argument("--out", default=str(ROOT / ".impeccable" / "shots"), help="output folder")
    parser.add_argument("--name", help="file name stem (only with a single path)")
    parser.add_argument("--wait", type=int, default=4000, help="virtual time budget in ms (default 4000)")
    parser.add_argument("--scale", type=float, default=1.0, help="device scale factor (default 1)")
    parser.add_argument("--port", type=int, default=0, help="port for the local server (default: a free one)")
    parser.add_argument("--browser", help="path to msedge.exe or chrome.exe")
    parser.add_argument("--js", help="JavaScript to run on load before the picture, e.g. to open a panel or fill a field")
    parser.add_argument("--serve", action="store_true", help="keep the signed-in server running and print its address")
    args = parser.parse_args()

    browser = find_browser(args.browser)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="zehnms-shoot-", ignore_cleanup_errors=True) as tmp:
        workdir = Path(tmp)
        settings = boot_django(workdir)
        if args.anon:
            from django.test import Client

            client = Client()
        else:
            client = sign_in(args.username)

        bridge = Bridge(client, settings)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(bridge))
        server.daemon_threads = True
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{port}"

        failures = 0
        for index, path in enumerate(args.paths):
            path = clean_path(path)
            stem = args.name if (args.name and len(args.paths) == 1) else slug_for(path)
            if args.js:
                bridge.inject[path] = args.js
            status, _headers, payload = bridge.fetch("GET", path)
            (out_dir / f"{stem}.html").write_bytes(payload)
            print(f"{path} -> HTTP {status}, {len(payload)} bytes")
            for label, (width, height) in SIZES.items():
                if args.only and args.only != label:
                    continue
                if args.full:
                    height = args.height
                target = out_dir / f"{stem}-{label}.png"
                shot_url = origin + path
                if width < MIN_WINDOW:
                    shot_url = f"{origin}{FRAME_URL}?w={width}&h={height}&u={quote(path, safe='')}"
                ok = capture(browser, shot_url, target, width, height, args.wait, args.scale,
                             workdir / f"profile-{index}-{label}")
                print(f"  {label:8s} {width}x{height}  {'saved ' + str(target) if ok else 'FAILED'}")
                failures += 0 if ok else 1

        if args.serve:
            print(f"Serving the signed-in copy at {origin}  (Ctrl+C to stop)")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
        server.shutdown()
        from django.db import connections

        connections.close_all()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
