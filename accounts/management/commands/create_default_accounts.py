from django.core.management.base import BaseCommand

from accounts.models import Account


class Command(BaseCommand):
    help = "Create default chart of accounts"

    def handle(self, *args, **options):
        accounts = [
            ("Cash", Account.ASSET),
            ("Bank", Account.ASSET),
            ("Accounts Receivable", Account.ASSET),
            ("Inventory", Account.ASSET),
            ("Accounts Payable", Account.LIABILITY),
            ("Sales Tax Payable", Account.LIABILITY),
            ("Expense Payable", Account.LIABILITY),
            ("Loan Payable", Account.LIABILITY),
            ("Sales", Account.INCOME),
            ("Cost of Goods Sold", Account.EXPENSE),
            ("Purchase", Account.EXPENSE),
            ("Salaries Expense", Account.EXPENSE),
            ("Interest Expense", Account.EXPENSE),
        ]

        created = 0

        for title, acc_type in accounts:
            obj, is_created = Account.objects.get_or_create(
                title=title,
                defaults={"type": acc_type},
            )
            if is_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"Created: {title}"))
            else:
                self.stdout.write(f"Already exists: {title}")

        self.stdout.write(
            self.style.SUCCESS(f"Default accounts setup complete. New accounts: {created}")
        )


