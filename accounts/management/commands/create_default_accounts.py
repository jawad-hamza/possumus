from django.core.management.base import BaseCommand
from accounts.models import Account


class Command(BaseCommand):
    help = "Create default chart of accounts"

    def handle(self, *args, **options):
        accounts = [
            # Assets
            ("Cash", Account.ASSET),
            ("Bank", Account.ASSET),
            ("Accounts Receivable", Account.ASSET),

            # Liabilities
            ("Accounts Payable", Account.LIABILITY),
            ("Loan Payable", Account.LIABILITY),

            # Income
            ("Sales", Account.INCOME),

            # Expenses
            ("Purchase", Account.EXPENSE),
            ("Salaries Expense", Account.EXPENSE),
            ("Interest Expense", Account.EXPENSE),
        ]

        created = 0

        for title, acc_type in accounts:
            obj, is_created = Account.objects.get_or_create(
                title=title,
                defaults={"type": acc_type}
            )
            if is_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"Created: {title}"))
            else:
                self.stdout.write(f"Already exists: {title}")

        self.stdout.write(self.style.SUCCESS(
            f"Default accounts setup complete. New accounts: {created}"
        ))
