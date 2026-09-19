from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from pos.services.licensing import PRIVATE_KEY_FILENAME, PUBLIC_KEY_FILENAME, generate_keypair, license_dir

GITIGNORE_CONTENT = (
    "# The signing key mints licences. It must never be committed or shipped in the installer.\n"
    f"{PRIVATE_KEY_FILENAME}\n"
)


class Command(BaseCommand):
    help = (
        "Vendor side, run ONCE: create the Ed25519 key pair used for offline licence keys. "
        "public_key.pem ships with the app; private_key.pem stays with the vendor."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dir", default="", help="Folder for the key files (default: <project>/licensing).")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Replace an existing key pair. Every licence key issued so far stops working.",
        )

    def handle(self, *args, **options):
        target = Path(options["dir"]) if options["dir"] else license_dir()
        target.mkdir(parents=True, exist_ok=True)
        private_path = target / PRIVATE_KEY_FILENAME
        public_path = target / PUBLIC_KEY_FILENAME
        gitignore_path = target / ".gitignore"

        # Written first, so the private key is already ignored the moment it exists.
        if not gitignore_path.exists() or PRIVATE_KEY_FILENAME not in gitignore_path.read_text(encoding="utf-8"):
            with gitignore_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(GITIGNORE_CONTENT)

        if (private_path.exists() or public_path.exists()) and not options["force"]:
            raise CommandError(
                f"A key pair already exists in {target}. Re-creating it invalidates every licence key "
                "already issued. Pass --force only if that is really what you want."
            )

        private_pem, public_pem = generate_keypair()
        private_path.write_bytes(private_pem)
        public_path.write_bytes(public_pem)
        try:
            private_path.chmod(0o600)
        except OSError:
            pass

        self.stdout.write(self.style.SUCCESS(f"Public key  (ship with the app): {public_path}"))
        self.stdout.write(self.style.SUCCESS(f"Private key (keep secret):       {private_path}"))
        self.stdout.write(
            self.style.WARNING(
                "Back the private key up somewhere safe and offline. If it is lost you cannot issue keys "
                "for installs already in the field; if it leaks, anyone can."
            )
        )
