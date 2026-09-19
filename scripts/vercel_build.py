"""Vercel build step (vercel.json -> buildCommand).

Vercel's Django preset installs requirements.txt and runs collectstatic on its own.
This script does the two things it does not do:

1. create the shared cache table (throttling and login lockout count there), and
2. apply database migrations.

Migrations run for production builds only. Preview deployments usually share the
production database, and a half-reviewed branch must not alter its schema. Override with
ZEHNMS_MIGRATE_ON_BUILD=1 (always migrate) or ZEHNMS_MIGRATE_ON_BUILD=0 (never; run
`python manage.py migrate` yourself as described in docs/DEPLOYMENT.md).
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def manage(*args):
    print(f"[vercel_build] manage.py {' '.join(args)}", flush=True)
    subprocess.run([sys.executable, str(ROOT / "manage.py"), *args], cwd=str(ROOT), check=True)


def should_migrate():
    flag = os.getenv("ZEHNMS_MIGRATE_ON_BUILD", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    if flag in {"0", "false", "no", "off"}:
        return False
    return os.getenv("VERCEL_ENV", "") == "production"


def main():
    # settings.py keys its serverless behaviour off this variable; Vercel sets it, local dry runs do not.
    os.environ.setdefault("VERCEL", "1")

    if not os.getenv("DATABASE_URL", "").strip():
        sys.exit(
            "[vercel_build] DATABASE_URL is not set. Add a Postgres database (Neon or Supabase) to the "
            "Vercel project for the Production, Preview and Development environments, then redeploy."
        )
    if not os.getenv("DJANGO_SECRET_KEY", "").strip():
        sys.exit("[vercel_build] DJANGO_SECRET_KEY is not set in the Vercel project environment variables.")

    if importlib.util.find_spec("django") is None:
        if os.getenv("ZEHNMS_MIGRATE_ON_BUILD", "").strip().lower() in {"0", "false", "no", "off"}:
            print("[vercel_build] Django is not importable here; database steps skipped as requested.", flush=True)
            return
        sys.exit(
            "[vercel_build] Django is not importable by the build's python. Set ZEHNMS_MIGRATE_ON_BUILD=0 "
            "and run the one-off migrate command from docs/DEPLOYMENT.md instead."
        )

    manage("check", "--deploy", "--fail-level", "ERROR")
    # Idempotent and independent of app migrations, so previews on their own database work too.
    manage("createcachetable")
    if should_migrate():
        manage("migrate", "--noinput")
    else:
        print("[vercel_build] Skipping migrate (not a production build).", flush=True)


if __name__ == "__main__":
    main()
