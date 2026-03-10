# Deployment Guide

## Current verdict

This project is not ready to deploy to a client VPS exactly as it is today.

It is in a good local-development and internal testing state:

- `manage.py check` passes
- `manage.py test` passes
- major business workflows are wired and tested

But there are still production blockers that should be resolved before any client rollout.

## Current blockers

### 1. Development-only Django settings

Current project settings in [possumus/settings.py](/C:/Users/Jawad%20Hamza/Desktop/codex/possumus/possumus/settings.py) still use development values:

- hardcoded `SECRET_KEY`
- `DEBUG = True`
- empty `ALLOWED_HOSTS`
- no `SECURE_SSL_REDIRECT`
- no `SESSION_COOKIE_SECURE`
- no `CSRF_COOKIE_SECURE`
- no HSTS settings

These must be moved to environment-driven production settings before deployment.

### 2. Static files are not production-configured

The project only defines `STATIC_URL`. It does not yet define a production static-file target such as:

- `STATIC_ROOT`
- a `collectstatic` deployment step
- WhiteNoise or an Nginx static-file configuration

Without this, CSS and JavaScript delivery will be fragile or broken in production.

### 3. SQLite is still the default database

The current default database is SQLite in [possumus/settings.py](/C:/Users/Jawad%20Hamza/Desktop/codex/possumus/possumus/settings.py).

For a real commercial VPS deployment, PostgreSQL is the safer target because:

- concurrent write behavior is stronger
- backups and restores are easier to operationalize
- corruption/locking risk is lower than SQLite under multi-user load
- database maintenance is more standard on VPS infrastructure

### 4. Backup/restore is SQLite-only

Current backup logic in [backup_restore.py](/C:/Users/Jawad%20Hamza/Desktop/codex/possumus/accounts/services/backup_restore.py) explicitly supports only SQLite.

If production moves to PostgreSQL, this feature needs one of these upgrades:

- PostgreSQL dump/restore support
- documented server-side backup jobs outside the app
- restore restrictions so the UI does not offer unsupported restore flows

### 5. Runtime dependency file is not production-ready

The current [requirements.txt](/C:/Users/Jawad%20Hamza/Desktop/codex/possumus/requirements.txt) looks like a workstation environment dump, not a clean server runtime manifest.

Issues include:

- many unrelated packages
- Windows-only packages in a likely Linux VPS target
- no clear application dependency baseline for Django + DRF + PDF/reporting stack

A clean deployment dependency file should be prepared before rollout.

## Recommended production target

Suggested client VPS stack:

- Ubuntu 24.04 LTS
- Python 3.12
- PostgreSQL 16
- Gunicorn
- Nginx
- systemd service units
- TLS via Let's Encrypt
- daily automated database and media backups

## Minimum work before client deployment

### Configuration

1. Split settings into `development` and `production`, or load production values from environment variables.
2. Move `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, and database credentials to environment variables.
3. Add `CSRF_TRUSTED_ORIGINS` for the client domain.
4. Enable HTTPS-related settings for production.

### Static and media

1. Add `STATIC_ROOT`.
2. Run `collectstatic` during deployment.
3. Serve `static/` and `media/` through Nginx or WhiteNoise plus Nginx.
4. Decide how company logos and backup files are stored and backed up.

### Database

1. Migrate production to PostgreSQL.
2. Test migrations on a fresh PostgreSQL database.
3. Replace or supplement in-app SQLite backup logic.
4. Add scheduled database dumps.

### Operations

1. Add logging configuration.
2. Add error monitoring.
3. Add service restart and health-check procedures.
4. Define update, rollback, and backup restore procedures.

### Packaging

1. Replace the current `requirements.txt` with a clean runtime dependency file.
2. Pin the exact versions required to boot the app.
3. Verify Linux installation on a fresh server.

## Recommended go-live checklist

Use this checklist before handing the app to a client:

- production settings file or env-based config exists
- deploy check passes without security warnings
- PostgreSQL is configured and tested
- static files are collected and served correctly
- media uploads persist across deploys
- HTTPS is enabled
- admin login works on the real domain
- cashier login works on the real domain
- POS checkout works end to end
- purchase flow works end to end
- expense payment flow works end to end
- receipt PDF works on the server
- backup strategy is tested
- restore strategy is tested
- logs are accessible
- rollback instructions are documented

## What is already in good shape

The application itself is in a much better place than the deployment shell around it.

Current positives:

- business flows are integrated
- tests are passing
- role-based access exists
- audit trail exists
- backup concept exists
- accounting linkage across POS, purchase, parties, and expenses is present
- documentation for usage and workflows already exists

## Honest recommendation

If the question is:

- `Can this be used locally or for internal testing now?` -> yes
- `Can this be deployed to a client VPS safely today without more work?` -> no

The codebase is close enough that a production-hardening pass is practical, but that pass still needs to be done deliberately.
