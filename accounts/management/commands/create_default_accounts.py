from django.core.management.base import BaseCommand

from accounts.services.chart_of_accounts import ensure_default_accounts


class Command(BaseCommand):
    help = "Create default chart of accounts"

    def handle(self, *args, **options):
        created = ensure_default_accounts()
        for title in created:
            self.stdout.write(self.style.SUCCESS(f"Created: {title}"))
        self.stdout.write(self.style.SUCCESS(f"Default accounts setup complete. New accounts: {len(created)}"))
