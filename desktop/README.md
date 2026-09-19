# ZehnMS desktop build (Windows, one shop)

This folder turns the Django project into an offline Windows program that opens in its own
window, like any installed application: title **ZehnMS**, its own taskbar button and icon, no
browser tabs, no address bar. The shop never sees a URL. The shop PC needs no Python, no database
server and no internet connection.

| File | Purpose |
| --- | --- |
| `launcher.py` | Program entry point: prepares the data folder, migrates, serves with waitress in-process, opens the application window |
| `zehnms.spec` | PyInstaller recipe (templates, static files, migrations, fonts, licence public key, window libraries, icon) |
| `installer.iss` | Inno Setup script: per-user install, shortcuts, WebView2 runtime check, uninstaller that keeps the data |
| `build.ps1` | One command that builds the exe, smoke-tests the real window and builds the installer |
| `make_icon.py` | Draws the placeholder icon `zehnms.ico` |
| `zehnms.ico` | **Placeholder** icon (a rounded square with "ZM"); see [The icon](#the-icon) |
| `VERSION` | Version number stamped on the installer |

## Build it

Requirements on the build PC: Windows 10/11 x64, Python 3.12 to 3.14 (3.14 is what the project is
tested on), and [Inno Setup 6](https://jrsoftware.org/isdl.php) for the final `Setup.exe`
(`winget install JRSoftware.InnoSetup`).

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

The script:

1. installs `requirements-desktop.txt` into `.venv` (waitress, pywebview, pythonnet, PyInstaller),
2. checks that pywebview and pythonnet import on this Python,
3. runs `collectstatic` with the desktop settings,
4. runs PyInstaller, giving `desktop\dist\ZehnMS\` (`ZehnMS.exe`, `zehnms-cli.exe`, `_internal\`),
5. smoke test 1: starts `ZehnMS.exe` the way the shortcut does on a scratch data folder, waits for
   `/healthz/` 200 and for a window titled "ZehnMS", fetches the sign-in page, stops it and checks
   the process ended and the lock file is gone,
6. smoke test 2: runs `zehnms-cli.exe --self-test`, in which the app opens its window, loads the
   first page inside it, writes what it saw to `selftest.json` (strategy used, WebView2 version,
   page title, anything the page requested from outside this PC, timings) and closes itself; the
   build fails if the page did not load or asked the internet for anything,
7. runs `ISCC.exe` when Inno Setup is installed, giving `desktop\dist\installer\ZehnMS-Setup-<version>.exe`.

Options: `-Version 1.2.0`, `-SkipInstaller`, `-SkipSmokeTest`, `-FetchWebView2` (see
[WebView2 runtime](#webview2-runtime)). Without Inno Setup you still get the portable folder
`desktop\dist\ZehnMS\`, which can be zipped and run from anywhere.

If `licensing\public_key.pem` exists when you build, it is bundled and used to verify licence
files. Never ship `licensing\private_key.pem`; it is git-ignored and is not part of the bundle.

## The application window

The launcher tries three ways to show the app, in this order, and uses the first that works:

| Strategy | What it is | When it is used |
| --- | --- | --- |
| `webview` | [pywebview](https://pywebview.flowrl.com/) hosting the **Microsoft Edge WebView2 runtime**, the web engine Windows already has. Nothing is bundled: the build grows by about 5 MB (pywebview, pythonnet and two Microsoft interop DLLs), not by a browser. | Default. Needs the WebView2 runtime (Windows 11: always; Windows 10: nearly always) and .NET Framework 4.6.2+ (part of Windows 10/11). |
| `appmode` | Microsoft Edge, or Chrome, started with `--app=<url>` and its own profile under `webview-profile\`. Also a chromeless window. | WebView2 runtime or pywebview missing, or WebView2 failed to start. The title is the page title ("Sign In \| ZehnMS") and the taskbar icon is the browser's. |
| `browser` | A tab in the default browser. | Last resort. The server then runs until **Stop ZehnMS**. |

Force where the chain starts with `--window webview|appmode|browser|none`, the environment
variable `ZEHNMS_WINDOW`, or `"window"` in `config.json` (that order of precedence). A forced
strategy that fails still falls through to the next one: a shop is better served by another kind
of window than by an error. `--no-window` (old name `--no-browser`) serves without opening anything.

pywebview needs pythonnet on Windows. Both install and run on Python 3.14 (pywebview 6.2.1,
pythonnet 3.1.0, which ships one wheel for Python 3.10 to 3.14), so the build uses the project's
normal `.venv`; no second interpreter is needed. If a future Python has no pythonnet wheel yet,
build with an older one (`py -3.13 -m venv .venv`) rather than dropping the window.

### Behaviour

- **Splash first.** The window appears with an inline "Opening your shop" page while Django starts
  on another thread, then loads the app. On the very first run, when the database is created, the
  shop sees a window instead of nothing.
- **Size and position** are remembered in `window.json` (saved when the window closes). Default
  1366 x 860, minimum 1024 x 640, maximised on screens smaller than 1500 x 940 (the usual
  1366 x 768 shop laptop). A saved position on a monitor that is no longer there is ignored.
- **Closing the window stops ZehnMS**: the waitress server ends and `zehnms.lock` is removed.
  For a PC that serves other tills, `"keep_running_after_close": true` in `config.json` keeps
  serving until **Stop ZehnMS**; starting ZehnMS again then just opens a window onto it.
- **Starting ZehnMS twice** brings the existing window to the front (the lock file records the
  port, the window strategy and the process that owns the window). Two double-clicks in a row do
  not start two servers: a named mutex makes the second copy wait for the first.
- **Downloads** (CSV and PDF exports) open a Windows **Save As** dialog.
- **Printing**: `window.print()`, which the receipt and report pages call, works in WebView2 and
  shows the same print preview as Edge, including the thermal printer's paper sizes. Ctrl+P does
  the same.
- **Keys**: a chromeless window has no toolbar, so the launcher adds F5 / Ctrl+R (reload),
  Alt+Left / Alt+Right (back, forward) and Ctrl+P (print). In-app links that ask for a new window
  (`target="_blank"`) open in the same window; a browser tab would show a URL and would not be
  signed in. Links to other sites open in the default browser.
- **Release vs debug**: the right-click menu and developer tools are off. `--debug` turns them
  on (F12).
- The sign-in cookie and the HTTP cache live in `webview-profile\`, so the shop stays signed in
  between runs the way it would in a browser.

### Performance

The server is in the same process as the window (waitress on a thread, loopback only), so there is
no extra hop and nothing to start besides `ZehnMS.exe`. Waitress runs 8 threads (12 in LAN mode;
`"threads"` in `config.json` overrides): one till holds about six connections while a page loads,
and idle threads only wait on a queue. Nothing polls while idle: `--stop` is delivered through a
Windows named event, the window strategies block in the OS until the window closes, and the
server loop is ended from inside by a waitress trigger.

Measured on the build PC (Windows 11, WebView2 153, frozen `ZehnMS.exe`, timed from process start
by an outside stopwatch):

| | Window visible | `/healthz/` 200 | First page painted |
| --- | --- | --- | --- |
| Very first run (creates the database, 71 migrations) | 3.7 s | 5.4 s | not timed |
| Every later run | 1.1 to 1.2 s | 1.3 to 1.5 s | about 1.2 s (launcher log) |

A page load inside the window takes about 25 ms to the load event. Idle, the launcher process
used 16 ms of CPU in 30 s. The launcher writes its own timings to `logs\launcher.log` on every
start ("Window shown after ...", "ready after ...", "First page shown after ...").

### WebView2 runtime

Windows 11 includes it; Windows 10 has had it pushed by Windows Update and by Edge since 2021.
For the rest:

- `installer.iss` checks the registry values Microsoft documents (`pv` under
  `EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` in HKLM 32/64-bit and HKCU).
- `build.ps1 -FetchWebView2` downloads Microsoft's Evergreen Bootstrapper (about 2 MB) to
  `desktop\redist\MicrosoftEdgeWebview2Setup.exe`, checks its Microsoft signature, and the
  installer then carries it and runs it (`/silent /install`) only where the runtime is missing.
  That folder is git-ignored; the bootstrapper itself downloads the runtime, so it needs internet
  once on that PC.
- Without the bootstrapper the installer shows a message, and ZehnMS opens in an Edge app window,
  which needs nothing extra. Installing the runtime later switches it to the standard window.

### The icon

`zehnms.ico` is a **placeholder** drawn by `make_icon.py` (dark rounded square, "ZM", 16 to 256 px)
because the product has no logo yet. It is used by `ZehnMS.exe`, the window and taskbar button,
the installer and the shortcuts. Replace the file with the real artwork and rebuild; nothing else
changes.

## What the shop gets

- Install is per user (no administrator prompt) into `%LOCALAPPDATA%\Programs\ZehnMS`.
- Shortcuts: **ZehnMS** (Start Menu, optional desktop icon), **Stop ZehnMS** and **Reset Owner Password**.
- Double-clicking **ZehnMS** opens the application window. The local server listens on
  `127.0.0.1:8765`; if that port is taken, the next free one is used. The shop never needs to know.
- **Stop ZehnMS** is only needed for a copy that runs without a window (LAN server kept running,
  or the browser fallback). Shutting down Windows also ends it safely.
- **Reset Owner Password** opens a console window and prints a temporary password for the owner
  account. It is the forgot-password route on a PC with no email. Team members' passwords are reset
  by an admin in Settings > Team.
- Bootstrap, Bootstrap Icons and Chart.js ship inside the app (`pos/static/vendor/`), so every page
  works with no internet connection. The only template that references a CDN is the marketing
  landing page (`pos/templates/pos/landing.html`, Google Fonts), which the desktop edition never
  serves: it goes straight to sign-in.

### Where the data lives

Everything the shop creates is in `%LOCALAPPDATA%\ZehnMS`, separate from the program:

| Path | Content |
| --- | --- |
| `db.sqlite3` | The database (WAL mode; `-wal`/`-shm` files next to it are normal) |
| `media\` | Uploaded logos |
| `backups\` | In-app backups and `auto-before-update-*.sqlite3` safety copies (newest 5 kept) |
| `logs\` | `application.log`, `launcher.log` |
| `secret_key.txt` | Generated on first run; keeps sign-ins valid across restarts |
| `zehnms.lock` | Present while the server runs (pid, port, URLs, window strategy, window process) |
| `window.json` | Last window size, position and maximised state |
| `webview-profile\` | The window's cookies and cache (`webview2\`, or `msedge\` / `chrome\` in app mode). Safe to delete while ZehnMS is closed; the shop signs in again. |
| `config.json` | Optional, see below |
| `zehnms.env` | Optional extra settings in `.env` format (for example SMTP) |

Uninstalling or upgrading never deletes this folder. To move a shop to a new PC, install
ZehnMS there and copy the folder across before the first start (`webview-profile\` can be left
behind).

`config.json` keys: `lan`, `port`, `allowed_hosts`, `threads`, `window`
(`webview`, `appmode`, `browser`, `none`), `keep_running_after_close`.

### Upgrades

Run the newer `ZehnMS-Setup-x.y.z.exe` over the old one. The installer stops the running
copy first. On the next start the launcher copies the database to
`backups\auto-before-update-<timestamp>.sqlite3` and then applies the new migrations.

### A second till on the same network

Create `%LOCALAPPDATA%\ZehnMS\config.json`:

```json
{ "lan": true, "port": 8765, "keep_running_after_close": true }
```

Restart ZehnMS. It now listens on the network; the addresses other tills should open are written
to `zehnms.lock` (`lan_urls`) and printed by `zehnms-cli.exe --status`. The second till uses its
own browser (or Edge's "Install this site as an app" for a chromeless window). Windows Firewall
asks once whether to allow it; choose "Private networks". Give the server PC a fixed IP address in
the router so the address does not change. Extra host names can be allowed with
`"allowed_hosts": ["till-server"]`. Without `keep_running_after_close`, closing the window on the
server PC also disconnects the second till.

This is plain http inside the shop's own network. Do not expose the port to the internet; use
the cloud deployment for that.

### Support commands

`zehnms-cli.exe` is the same program with a console window:

```powershell
cd "$env:LOCALAPPDATA\Programs\ZehnMS"
.\zehnms-cli.exe --status
.\zehnms-cli.exe --stop
.\zehnms-cli.exe --window appmode
.\zehnms-cli.exe --debug
.\zehnms-cli.exe --lan --no-window
.\zehnms-cli.exe --manage reset_owner_password
.\zehnms-cli.exe --manage showmigrations
```

`--data-dir <folder>` points any of these at a different data folder (used by the smoke test).

## Run the launcher from source

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe desktop\launcher.py --data-dir .\build\dev-desktop-data
```

This exercises exactly what the installed copy does (desktop settings, waitress, WhiteNoise, the
WebView2 window) without building anything. Without pywebview installed it opens an Edge app
window instead.

## Known limits

- Unsigned executables trigger a SmartScreen warning ("More info" > "Run anyway"). Buy a code
  signing certificate and sign `ZehnMS.exe` and the installer before wide distribution.
- The icon is a placeholder.
- The window title is always "ZehnMS" in the `webview` strategy; it does not follow the page title.
- After the window closes, `ZehnMS.exe` lingers for about two seconds while Windows unloads the
  WebView2 host. The server is already stopped and the lock released, so starting ZehnMS again
  straight away works.
- In the `appmode` fallback, downloads go to the browser's Downloads folder instead of a Save As
  dialog, and the taskbar shows the browser's icon.
