from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from pos.services.licensing import (
    PRIVATE_KEY_FILENAME,
    LicenseError,
    build_payload,
    license_dir,
    load_private_key,
    sign_license,
)
from pos.services.modules import EDITIONS, MODULE_KEYS, ModuleError, edition_modules, parse_module_list


class Command(BaseCommand):
    help = "Vendor side: sign an offline licence key for one customer using the private key file."

    def add_arguments(self, parser):
        parser.add_argument("--licensee", required=True, help="Shop or person the licence is for.")
        parser.add_argument("--email", default="", help="Customer email, for your records.")
        parser.add_argument("--plan", default="standard", help="Plan name stored in the key (default: standard).")
        duration = parser.add_mutually_exclusive_group(required=True)
        duration.add_argument("--days", type=int, help="Licence length in days, counted from now.")
        duration.add_argument("--perpetual", action="store_true", help="Licence that never expires.")
        parser.add_argument("--max-users", type=int, default=None, help="Active user limit (default: unlimited).")
        parser.add_argument(
            "--edition",
            default="",
            help=(
                f"Edition preset that sets the feature ceiling: {', '.join(EDITIONS)}. "
                "Without --edition or --modules the key includes everything."
            ),
        )
        parser.add_argument(
            "--modules",
            default=None,
            help=(
                "Comma list of modules the key includes, overriding the edition preset. "
                f"Choose from: {', '.join(MODULE_KEYS)}."
            ),
        )
        parser.add_argument("--private-key", default="", help="Path to private_key.pem (default: <project>/licensing).")
        parser.add_argument("--out", default="", help="Also write the key to this file.")

    def handle(self, *args, **options):
        # Checked before the private key is touched, so a typo fails fast and signs nothing.
        edition = (options["edition"] or "").strip().lower()
        try:
            modules = edition_modules(edition) if edition else None
            if options["modules"] is not None:
                modules = parse_module_list(options["modules"])
                if not modules:
                    raise ModuleError("--modules needs at least one module name. Leave it out for a key with everything.")
        except ModuleError as exc:
            raise CommandError(str(exc)) from exc

        private_path = Path(options["private_key"]) if options["private_key"] else license_dir() / PRIVATE_KEY_FILENAME
        if not private_path.is_file():
            raise CommandError(f"No private key at {private_path}. Run `manage.py license_keygen` on the vendor machine first.")

        try:
            private_key = load_private_key(private_path)
            payload = build_payload(
                licensee=options["licensee"],
                email=options["email"],
                plan=options["plan"],
                days=None if options["perpetual"] else options["days"],
                max_users=options["max_users"],
                edition=edition,
                modules=modules,
            )
        except (LicenseError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        key = sign_license(payload, private_key)
        if options["out"]:
            Path(options["out"]).write_text(key + "\n", encoding="ascii")

        self.stderr.write(
            f"Licence for {payload['licensee']} | plan {payload['plan'] or '-'} | "
            f"expires {payload['expires_at'] or 'never'} | users {payload['max_users'] or 'unlimited'} | "
            f"edition {payload.get('edition') or '-'} | "
            f"modules {', '.join(payload['modules']) if payload.get('modules') is not None else 'all'}"
        )
        # The key alone goes to stdout so it can be piped or copied cleanly.
        self.stdout.write(key)
