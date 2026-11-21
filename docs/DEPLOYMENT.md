# Deployment guide

One install or deployment serves **one shop**. Pick the target that fits:

| Target | Who it is for | Database | Served by | Section |
| --- | --- | --- | --- | --- |
| Local development | Developers | SQLite | `manage.py runserver` | [1](#1-local-development) |
| Windows desktop installer | A shop with one PC (plus optional extra tills on the LAN), offline | SQLite in `%LOCALAPPDATA%\ZehnMS` | waitress, started by `ZehnMS.exe` | [2](#2-windows-desktop-installer) |
| Vercel | A shop that wants the app online without running a server | Hosted Postgres (Neon, Supabase) | Vercel Functions (Django preset) | [3](#3-vercel) |
| VPS with Docker (recommended for a VPS) | Full control, in-app backups, next to other apps on the same server | PostgreSQL 17 container | gunicorn container behind the host's nginx | [4](#4-vps-with-docker) |
| VPS without Docker | Full control on a dedicated server | PostgreSQL | gunicorn behind nginx | [5](#5-vps-gunicorn--postgresql--nginx) |

All configuration is done with environment variables. `.env.example` lists every one of them,
grouped, with defaults. A file named `.env` next to `manage.py` is read automatically
(real environment variables win; the test suite ignores it).

Two switches change behaviour broadly:

- `DEPLOYMENT_MODE=desktop|cloud` (default `cloud`). Desktop mode means plain http on
  localhost, no online checkout, Django serves uploaded files, SQLite in WAL mode.
- `VERCEL` (set by Vercel itself). Nothing is written to disk: console logging only, in-app
  backups off, database-backed cache, debug off, proxy headers trusted.

The app **refuses to start** when `DJANGO_DEBUG` is off and `DJANGO_SECRET_KEY` is missing.
Generate one with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

`GET /healthz/` is public and returns `{"status": "ok", "db": "ok"}` (503 when the database
cannot be reached). Use it for uptime monitors and load balancer checks.

---

## 1. Local development

Python 3.12 or newer (the project is developed and tested on 3.14).

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000/ and create the owner account. With no configuration you get
debug mode, SQLite (`db.sqlite3`), emails printed to the console, uploads in `media/`.

```powershell
.\.venv\Scripts\python.exe manage.py test          # full suite
.\.venv\Scripts\python.exe manage.py test pos.test_platform
python -m pyflakes pos accounts api possumus desktop scripts
```

To try production behaviour locally, run the desktop launcher from source
(`python desktop/launcher.py --data-dir build/dev-desktop-data`); it uses waitress,
WhiteNoise and debug-off settings.

---

## 2. Windows desktop installer

Build on a Windows PC:

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

Result: `desktop\dist\installer\ZehnMS-Setup-<version>.exe` (needs Inno Setup 6 on the build
PC; without it you still get the portable folder `desktop\dist\ZehnMS\`).

What the shop experiences: run the installer (no administrator rights), click **ZehnMS**, and the
program opens in its own window titled ZehnMS with its own taskbar button. There are no browser
tabs and no address bar, and the shop never sees a URL. Create the owner account, start selling.
Closing the window stops the program; starting it again while it is open brings that window to the
front. The `TRIAL_DAYS` setting (default 10) and the bundled `licensing/public_key.pem` drive the
trial and licence check.

How the window is made, in the order the launcher tries:

1. **webview**: pywebview hosting the Microsoft Edge WebView2 runtime that Windows 11 and nearly
   every Windows 10 PC already has. No browser is bundled; this adds about 5 MB to the build.
2. **appmode**: Microsoft Edge (or Chrome) started with `--app=`, also a chromeless window. Used
   when the WebView2 runtime or pywebview is missing.
3. **browser**: a tab in the default browser, as a last resort.

`ZEHNMS_WINDOW=webview|appmode|browser` (or `--window`, or `"window"` in `config.json`) sets where
that chain starts. The server stays inside the same process (waitress on `127.0.0.1:8765`, 8
threads, 12 in LAN mode), so the window costs no extra hop. The installer checks for the WebView2
runtime and, if `desktop\redist\MicrosoftEdgeWebview2Setup.exe` was present at build time
(`build.ps1 -FetchWebView2`), installs it where it is missing.

Data lives in `%LOCALAPPDATA%\ZehnMS` and survives upgrades and uninstalls. Before an upgrade
changes the database the launcher saves `backups\auto-before-update-<timestamp>.sqlite3`.

Everything else (measured start-up times, printing and downloads in the window, second till over
the LAN, support commands, moving to another PC, the placeholder icon, code signing) is in
[desktop/README.md](../desktop/README.md).

---

## 3. Vercel

Vercel detects Django from `manage.py`, reads `WSGI_APPLICATION` from settings, installs
`requirements.txt`, runs `collectstatic` by itself and serves `/static/` from its CDN. The whole
app becomes one Vercel Function (`possumus/wsgi.py`).

Repository files involved:

| File | Role |
| --- | --- |
| `vercel.json` | Pins the `django` framework preset, sets the build command, function duration (60 s) and bundle excludes |
| `.python-version` | `3.14`. Vercel offers 3.12 (default), 3.13 and 3.14; Django 6.0 needs 3.12 or newer, and 3.14 is what the test suite runs on |
| `scripts/vercel_build.py` | Build step: `check --deploy`, `createcachetable`, and `migrate` on production builds |
| `.vercelignore` | Keeps the virtualenv, local database, docs and desktop build out of the upload |

About the `api/` folder: Vercel's legacy behaviour turns every `.py` file under `/api` into its
own function. A detected framework preset takes precedence, and `vercel.json` pins
`"framework": "django"` so this can never be left to auto-detection. `/api/...` requests reach
Django's URL router like any other path. Do not add a `builds` section to `vercel.json`; it
switches the preset off.

### Steps

1. **Database.** Create a Postgres database (Vercel Marketplace > Neon or Supabase, or your own).
   Use the **pooled** connection string (Neon: host contains `-pooler`; Supabase: port 6543).
2. **Project.** Import the Git repository in Vercel, or from the project folder:
   ```bash
   npm i -g vercel        # CLI 50.38.0 or newer
   vercel link
   ```
3. **Environment variables** (Project > Settings > Environment Variables, for Production *and*
   Preview; the build needs them too):

   | Variable | Value |
   | --- | --- |
   | `DJANGO_SECRET_KEY` | Required. Long random string |
   | `DATABASE_URL` | Required. `postgresql://user:pass@host/db?sslmode=require` (set automatically by the Neon/Supabase integration) |
   | `DJANGO_ALLOWED_HOSTS` | Only for a custom domain: `shop.example.com` |
   | `DJANGO_CSRF_TRUSTED_ORIGINS` | Only for a custom domain: `https://shop.example.com` |
   | `PUBLIC_APP_DOMAIN` | `https://shop.example.com` (used in emails) |
   | `DJANGO_EMAIL_HOST`, `DJANGO_EMAIL_HOST_USER`, `DJANGO_EMAIL_HOST_PASSWORD`, `DJANGO_DEFAULT_FROM_EMAIL` | SMTP account; owner sign-up sends a verification email |
   | `AWS_STORAGE_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_S3_ENDPOINT_URL`, `AWS_S3_REGION_NAME` | Needed for logo uploads (any S3-compatible store; Supabase Storage works) |
   | `LEMON_SQUEEZY_*`, `GOOGLE_OAUTH_*` | Optional, see `.env.example` |

   You do **not** set `DJANGO_DEBUG`, `TRUST_PROXY`, `CACHE_BACKEND`, `DJANGO_FILE_LOGGING` or
   the `*.vercel.app` host names: on Vercel they default to off, on, `db`, off, and the
   deployment's own host names are allowed automatically.
4. **Deploy.**
   ```bash
   vercel deploy          # preview
   vercel deploy --prod   # production (runs migrations)
   ```
5. Open the site, create the owner account, set up the business.

### Migrations

Production builds run `python manage.py migrate --noinput` from `scripts/vercel_build.py`.
Preview builds do not, because previews normally share the production database. Control it
with `ZEHNMS_MIGRATE_ON_BUILD` (`1` = always, `0` = never).

One-off command from your own PC (also the fallback if the build cannot reach the database):

```bash
vercel env pull .env.local
ZEHNMS_ENV_FILE=.env.local python manage.py createcachetable
ZEHNMS_ENV_FILE=.env.local python manage.py migrate
```

```powershell
vercel env pull .env.local
$env:ZEHNMS_ENV_FILE = ".env.local"
python manage.py createcachetable
python manage.py migrate
Remove-Item Env:ZEHNMS_ENV_FILE
```

`.env.local` is git-ignored. Delete it afterwards; it contains production secrets.

### What is different on serverless

| Feature | Behaviour on Vercel |
| --- | --- |
| In-app backup / restore | Off (`BACKUPS_AVAILABLE=False`): no disk and no `pg_dump`. Use your database provider's backups / point-in-time restore |
| Uploaded logos | Need S3-compatible storage. Without it uploads land in `/tmp` and disappear |
| Log files | None; everything goes to stdout, visible in Vercel's runtime logs. Add `SENTRY_DSN` (and `sentry-sdk` to `requirements.txt`) for error tracking |
| Cache, throttling, login lockout | Stored in the database table `zehnms_cache` so all function instances share it. Set `REDIS_URL` (and add `redis` to `requirements.txt`) for something faster |
| Database connections | Opened per request (`DB_CONN_MAX_AGE=0`), server-side cursors off; this is why the pooled connection string matters |
| Request size | Vercel caps request bodies at 4.5 MB, so large uploads fail regardless of Django's limits |
| Long requests | Cut off after 60 s (`maxDuration` in `vercel.json`) |
| Invoices (PDF), barcodes | Generated in memory; work unchanged |

---

## 4. VPS with Docker

The app and its own PostgreSQL run as two containers (`compose.yaml`, `Dockerfile`). The app
listens on `127.0.0.1:3400` only, and the host's nginx publishes it with a Let's Encrypt
certificate: the same pattern as the other sites on a shared VPS. Tested with Docker 29 and
Compose v5; the target server runs Ubuntu 26.04.

| File | Role |
| --- | --- |
| `Dockerfile` | Python 3.14 slim image: requirements, `collectstatic`, `pg_dump` for in-app backups, non-root user, health check |
| `compose.yaml` | `web` (gunicorn) + `db` (`postgres:17-alpine`); read-only app filesystem, no capabilities, the database on an internal network with no outside access, named volumes for data |
| `deploy/vps-deploy.sh` | On the server: first-run `.env` / `db.env` with random secrets, backup, build, start, wait until healthy |
| `deploy/upload.ps1` | On Windows: `git archive` of the last commit, upload, run `vps-deploy.sh` |
| `deploy/backup.sh` | Database dump + uploads archive into `/srv/zehnms/backups`, 14 days kept |
| `deploy/nginx/zehnms.conf` | nginx site (http to https, TLS, Cloudflare real IP, access log without query strings) |
| `deploy/gunicorn.conf.py` | gunicorn settings (workers, threads, timeouts, logging to stdout) |
| `deploy/env.vps.example` | Template of `/srv/zehnms/.env` |

Layout on the server:

```
/srv/zehnms/app/        the code (replaced on every upload; holds no data)
/srv/zehnms/.env        app settings, mode 600 (secret key, domain, SMTP, DB password)
/srv/zehnms/db.env      POSTGRES_PASSWORD, mode 600
/srv/zehnms/backups/    nightly and pre-deploy dumps
Docker volumes          zehnms_pgdata (database), zehnms_media (uploads), zehnms_appbackups (in-app backups)
```

Django serves `/media/` itself in this setup (`DJANGO_SERVE_MEDIA=true` in `compose.yaml`): that
view keeps bank receipts behind a sign-in, so nginx never needs access to the files.

### First deployment

1. DNS: point an `A` record for the shop's domain at the server.
2. On the server, once (your user must be in the `docker` group):
   ```bash
   sudo install -d -o $USER -g $USER -m 750 /srv/zehnms
   ```
3. On Windows, commit your work, then:
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\upload.ps1 -Domain shop.example.com
   ```
   (`-SshHost` defaults to `hostinger-vps`, the `~/.ssh/config` entry.) This uploads the
   committed code, writes `/srv/zehnms/.env` and `db.env`, builds the image, starts both
   containers (the app migrates on start) and waits for `/healthz/`.
4. On the server, put the SMTP settings into `/srv/zehnms/.env`, then
   `cd /srv/zehnms/app && docker compose up -d`.
5. nginx and https (the deploy script prints these lines with your domain filled in):
   ```bash
   sudo certbot certonly --nginx -d shop.example.com
   sed "s|__DOMAIN__|shop.example.com|g" /srv/zehnms/app/deploy/nginx/zehnms.conf | sudo tee /etc/nginx/sites-available/zehnms
   sudo ln -s /etc/nginx/sites-available/zehnms /etc/nginx/sites-enabled/zehnms
   sudo nginx -t && sudo systemctl reload nginx
   curl -s https://shop.example.com/healthz/
   ```
   Behind Cloudflare, the HTTP-01 challenge needs plain http to reach the server for
   `/.well-known/acme-challenge/` ("Always Use HTTPS" off, or that path exempt), and the SSL
   mode must be "Full (strict)".
6. Nightly backups (as your user, no sudo):
   ```bash
   (crontab -l 2>/dev/null; echo '30 2 * * * bash /srv/zehnms/app/deploy/backup.sh nightly >> /srv/zehnms/backups/backup.log 2>&1') | crontab -
   ```
7. Open the site, create the owner account, set up the business.

Without Windows, clone or copy the repository to `/srv/zehnms/app` and run
`bash /srv/zehnms/app/deploy/vps-deploy.sh --domain shop.example.com`.

### Without a domain (IP address only, plain http)

Until a domain exists, ZehnMS can be reached at `http://<server-ip>:<port>`. There is no
certificate, so passwords and business data cross the network unencrypted: use it for setup and
testing, and move to a domain + https before real use. Port 80 on the IP usually belongs to the
server's existing default site, so ZehnMS gets its own port (8090 below).

1. Run the first deployment with the IP as the "domain":
   `bash /srv/zehnms/app/deploy/vps-deploy.sh --domain 203.0.113.5`
2. In `/srv/zehnms/.env`, edit these lines in place (do not add second copies) to use the http address and switch the https-only settings off:
   ```ini
   DJANGO_ALLOWED_HOSTS=203.0.113.5
   DJANGO_CSRF_TRUSTED_ORIGINS=http://203.0.113.5:8090
   PUBLIC_APP_DOMAIN=http://203.0.113.5:8090
   DJANGO_SECURE_SSL_REDIRECT=false
   DJANGO_SESSION_COOKIE_SECURE=false
   DJANGO_CSRF_COOKIE_SECURE=false
   DJANGO_SECURE_HSTS_SECONDS=0
   ```
   then `cd /srv/zehnms/app && docker compose up -d`. (`check --deploy` now warns about https;
   that is expected in this mode.)
3. nginx on its own port, and open that port (ufw, and the provider's firewall panel if it has one):
   ```bash
   sed "s|__PORT__|8090|g" /srv/zehnms/app/deploy/nginx/zehnms-ip.conf | sudo tee /etc/nginx/sites-available/zehnms
   sudo ln -s /etc/nginx/sites-available/zehnms /etc/nginx/sites-enabled/zehnms
   sudo nginx -t && sudo systemctl reload nginx
   sudo ufw allow 8090/tcp
   curl -s http://203.0.113.5:8090/healthz/
   ```
   Never publish the container port itself on `0.0.0.0`: Docker's port publishing bypasses ufw.

Moving to a domain later: point its DNS at the server, put the domain into
`DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` / `PUBLIC_APP_DOMAIN` (https), remove the
four https overrides (or set them back to true / 31536000), `docker compose up -d`, then follow step 5 above with `zehnms.conf`
(replacing the IP site) and close port 8090 again.

### Updates

Commit, then run `deploy\upload.ps1` again (no `-Domain`). Every deploy dumps the database
first (`zehnms-db-<time>-pre-deploy.dump`), keeps the running image as `zehnms:previous`,
builds the new one and migrates on start.

### Day-to-day commands (in `/srv/zehnms/app`)

```bash
docker compose ps                         # state and health
docker compose logs -f web                # app + gunicorn log
docker compose exec web python manage.py check --deploy
docker compose up -d                      # after editing /srv/zehnms/.env
bash deploy/backup.sh manual              # backup now
```

### Rolling back

Code only (no migration ran, or the migration is backwards compatible):

```bash
cd /srv/zehnms/app
docker tag zehnms:previous zehnms:latest
docker compose up -d --no-build web
```

Code and database: restore the pre-deploy dump as well (below), then roll the code back.

### Restoring a backup

```bash
cd /srv/zehnms/app
docker compose stop web
docker compose exec -T db pg_restore -U zehnms -d zehnms --clean --if-exists --no-owner < /srv/zehnms/backups/zehnms-db-<time>.dump
docker compose run --rm --no-deps -T --entrypoint tar web -C /data -xzf - < /srv/zehnms/backups/zehnms-media-<time>.tar.gz
docker compose start web
```

Copy `/srv/zehnms/backups` off the server as well (rclone, restic or your provider's
snapshots): a backup on the same disk does not survive the disk.

### Sizing

The app container uses about 160 MB with 2 gunicorn workers x 4 threads and is capped at
768 MB; PostgreSQL idles at about 50 MB. Change `GUNICORN_WORKERS` / `GUNICORN_THREADS` in
`/srv/zehnms/.env`. To use another port, export `ZEHNMS_PORT` before `docker compose` and
change the `upstream` in the nginx site to match.

---

## 5. VPS (gunicorn + PostgreSQL + nginx)

Example for Ubuntu 24.04; adapt names and paths.

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip postgresql postgresql-client nginx
sudo -u postgres psql -c "CREATE USER zehnms WITH PASSWORD 'change-me';"
sudo -u postgres psql -c "CREATE DATABASE zehnms OWNER zehnms;"

sudo useradd --system --create-home --home-dir /srv/zehnms zehnms
sudo -u zehnms git clone <your-repo-url> /srv/zehnms/app
cd /srv/zehnms/app
sudo -u zehnms python3 -m venv .venv
sudo -u zehnms .venv/bin/pip install -r requirements-server.txt
```

`/srv/zehnms/app/.env` (owner `zehnms`, mode `600`):

```ini
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<long random string>
DJANGO_ALLOWED_HOSTS=shop.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://shop.example.com
DATABASE_URL=postgresql://zehnms:change-me@127.0.0.1:5432/zehnms
TRUST_PROXY=true
CACHE_BACKEND=db
ZEHNMS_DATA_DIR=/srv/zehnms/data
PUBLIC_APP_DOMAIN=https://shop.example.com
DJANGO_EMAIL_HOST=smtp.example.com
DJANGO_EMAIL_HOST_USER=...
DJANGO_EMAIL_HOST_PASSWORD=...
DJANGO_DEFAULT_FROM_EMAIL=shop@example.com
```

- `TRUST_PROXY=true` is required behind nginx. Without it Django does not believe the request
  arrived over https and the https redirect loops.
- `CACHE_BACKEND=db` makes login lockout and API throttling shared between gunicorn workers.
- `ZEHNMS_DATA_DIR` holds `media/`, `logs/` and `backups/`, outside the code checkout.

```bash
sudo -u zehnms mkdir -p /srv/zehnms/data
sudo -u zehnms .venv/bin/python manage.py migrate
sudo -u zehnms .venv/bin/python manage.py createcachetable
sudo -u zehnms .venv/bin/python manage.py collectstatic --noinput
sudo -u zehnms .venv/bin/python manage.py check --deploy
```

`/etc/systemd/system/zehnms.service`:

```ini
[Unit]
Description=ZehnMS (gunicorn)
After=network.target postgresql.service

[Service]
User=zehnms
Group=zehnms
WorkingDirectory=/srv/zehnms/app
ExecStart=/srv/zehnms/app/.venv/bin/gunicorn possumus.wsgi:application --bind 127.0.0.1:8000 --workers 3 --timeout 60
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/nginx/sites-available/zehnms`:

```nginx
server {
    listen 80;
    server_name shop.example.com;
    client_max_body_size 210m;   # in-app backup uploads

    # Bank receipts are private; only the app hands them out, after a sign-in check.
    location ^~ /media/bank_receipts/ {
        return 404;
    }

    location /media/ {
        alias /srv/zehnms/data/media/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        # Overwrite, never append: sign-in lockout trusts the first address in this header.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/zehnms /etc/nginx/sites-enabled/
sudo systemctl enable --now zehnms
sudo nginx -t && sudo systemctl reload nginx
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d shop.example.com      # adds the https server block
curl -s https://shop.example.com/healthz/
```

Static files are served by WhiteNoise from inside gunicorn (hashed names, one-year cache), so
nginx needs no `/static/` block.

### Updating

```bash
cd /srv/zehnms/app
sudo -u zehnms git pull
sudo -u zehnms .venv/bin/pip install -r requirements-server.txt
sudo -u zehnms .venv/bin/python manage.py migrate
sudo -u zehnms .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart zehnms
```

### Backups

- In the app: Settings > Backups creates `pg_dump` snapshots (needs `postgresql-client`, installed
  above). Restoring an uploaded SQL dump runs arbitrary SQL, so it stays disabled unless
  `ALLOW_SQL_RESTORE=true`.
- On the server, also schedule an independent dump and copy it off the machine:
  ```bash
  0 2 * * * pg_dump --format=custom zehnms > /srv/zehnms/dumps/zehnms-$(date +\%F).dump
  ```
  Include `/srv/zehnms/data/media/` in the same off-site copy.

---

## Go-live checklist

- [ ] `DJANGO_SECRET_KEY` is set and private; `DJANGO_DEBUG` is off
- [ ] `python manage.py check --deploy` reports no errors
- [ ] `/healthz/` returns 200 on the real address
- [ ] Owner sign-up email arrives (cloud) and sign-in works
- [ ] A sale, a purchase, a return and an invoice PDF work end to end
- [ ] A logo upload survives a redeploy (S3 on Vercel, `ZEHNMS_DATA_DIR` on a VPS)
- [ ] A backup has been taken **and restored once** on a scratch copy
- [ ] Desktop: installer tested on a clean Windows user account; upgrade keeps the data
