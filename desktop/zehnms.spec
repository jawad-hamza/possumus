# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the ZehnMS Windows desktop build.
#
#   pyinstaller desktop/zehnms.spec --noconfirm --distpath desktop/dist --workpath build/pyinstaller
#
# Produces desktop/dist/ZehnMS/ (one folder) with
#   ZehnMS.exe       windowed launcher used by the shortcuts; opens the app in its own window
#   zehnms-cli.exe   same program with a console (for --status, --stop, --lan and troubleshooting)
#
# Run `manage.py collectstatic` first (desktop/build.ps1 does) so staticfiles/ exists.

from pathlib import Path

import rest_framework
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve().parent
PROJECT_PACKAGES = ["possumus", "pos", "accounts", "api"]
SKIPPED_DIRS = {"__pycache__"}


def is_test_module(path):
    name = path.name
    return name == "tests.py" or name.startswith("test_") or name == "test_support.py"


def modules_on_disk(root, package):
    """Every module of a package, found by walking the folder instead of importing it.

    Django loads apps, migrations, template tags, management commands and middleware by
    dotted string, so PyInstaller's import scanner never sees them; and importing Django
    code here (what collect_submodules does) fails because settings are not configured.
    """
    modules = []
    for path in sorted((Path(root) / package).rglob("*.py")):
        if SKIPPED_DIRS.intersection(path.parts) or is_test_module(path):
            continue
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(parts))
    return modules


def project_modules():
    modules = []
    for package in PROJECT_PACKAGES:
        modules += modules_on_disk(PROJECT_ROOT, package)
    return modules


def project_data_files():
    """Templates, static sources and any other non-Python file that lives inside an app."""
    datas = []
    for package in PROJECT_PACKAGES:
        for path in sorted((PROJECT_ROOT / package).rglob("*")):
            if not path.is_file() or SKIPPED_DIRS.intersection(path.parts):
                continue
            if path.suffix in {".py", ".pyc", ".pyo"}:
                continue
            datas.append((str(path), str(path.parent.relative_to(PROJECT_ROOT))))
    return datas


def tree(source, target):
    source = PROJECT_ROOT / source
    if not source.exists():
        return []
    return [
        (str(path), str(Path(target) / path.parent.relative_to(source)))
        for path in sorted(source.rglob("*"))
        if path.is_file()
    ]


def keep_english_only(toc):
    """Django ships ~90 translations; the app runs in English, so drop the rest (tens of MB)."""
    kept = []
    for entry in toc:
        parts = Path(entry[0]).parts  # entry = (name inside the bundle, source path, type)
        if "locale" in parts[:-1]:
            position = parts.index("locale")
            language = parts[position + 1] if len(parts) > position + 2 else ""
            if language and language not in {"en", "en_GB", "en_AU"}:
                continue
        kept.append(entry)
    return kept


static_root = PROJECT_ROOT / "staticfiles"
if not (static_root / "staticfiles.json").is_file():
    raise SystemExit("staticfiles/ is missing. Run `python manage.py collectstatic --noinput` (or desktop/build.ps1) first.")

datas = []
datas += project_data_files()
datas += tree("staticfiles", "staticfiles")
datas += tree("static", "static")
datas += collect_data_files("rest_framework")
datas += collect_data_files("reportlab")
datas += collect_data_files("barcode")
datas += collect_data_files("tzdata")

# The application window: pywebview drives the Edge WebView2 runtime that Windows already has.
# What ships is pywebview, pythonnet and two small Microsoft interop DLLs (about 5 MB), not a
# browser. PyInstaller's own hooks for webview, pythonnet and clr_loader collect those DLLs. If
# pywebview is not installed the build still works and the launcher uses Edge app mode instead.
try:
    import webview  # noqa: F401
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False
    print("WARNING: pywebview is not installed; the build will open ZehnMS in Edge app mode.")

icon_file = PROJECT_ROOT / "desktop" / "zehnms.ico"
if icon_file.is_file():
    datas.append((str(icon_file), "desktop"))  # the window and taskbar icon (launcher.icon_path)

public_key = PROJECT_ROOT / "licensing" / "public_key.pem"
if public_key.is_file():
    datas.append((str(public_key), "licensing"))

hiddenimports = []
hiddenimports += project_modules()
hiddenimports += modules_on_disk(Path(rest_framework.__file__).resolve().parent.parent, "rest_framework")
hiddenimports += collect_submodules("whitenoise")
hiddenimports += collect_submodules("waitress")
hiddenimports += collect_submodules("barcode")
hiddenimports += collect_submodules("tzdata")
if HAS_WEBVIEW:
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]
hiddenimports += [
    "reportlab.graphics.barcode",
    "google.auth.transport.requests",
    "google.oauth2.id_token",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "cryptography.hazmat.primitives.serialization",
    "http.cookies",
    "html.parser",
]

# Present in developer virtualenvs but never needed on a till.
excludes = [
    "IPython", "jedi", "notebook", "nbconvert", "nbformat", "jupyter_client", "jupyter_core",
    "tornado", "zmq", "matplotlib", "numpy", "pandas", "tkinter", "pytest",
    "boto3", "botocore", "s3transfer", "storages",
    "pipreqs", "yarg", "pyflakes", "ruff", "PyInstaller",
    "psycopg", "psycopg_binary", "MySQLdb", "pymysql", "oracledb", "cx_Oracle",
    # pywebview back ends for other systems and engines; Windows uses winforms + edgechromium.
    "webview.platforms.android", "webview.platforms.cocoa", "webview.platforms.gtk",
    "webview.platforms.qt", "webview.platforms.cef", "webview.platforms.mshtml",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3", "jnius", "android",
]

a = Analysis(
    [str(PROJECT_ROOT / "desktop" / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
a.datas = keep_english_only(a.datas)
# pywebview also carries its Android runtime; a till never needs it.
a.datas = [entry for entry in a.datas if not entry[0].replace("\\", "/").endswith("pywebview-android.jar")]

pyz = PYZ(a.pure)

icon = str(icon_file) if icon_file.is_file() else None

windowed_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ZehnMS",
    console=False,
    icon=icon,
    upx=False,
)
console_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="zehnms-cli",
    console=True,
    icon=icon,
    upx=False,
)

coll = COLLECT(
    windowed_exe,
    console_exe,
    a.binaries,
    a.datas,
    upx=False,
    name="ZehnMS",
)
