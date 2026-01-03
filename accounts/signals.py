from django.db.models.signals import post_save
from django.dispatch import receiver
from accounts.models import AccountingEvent , Settlement
from accounts.services.settlements import post_settlement
from accounts.services.event_processor import process_event


@receiver(post_save, sender=AccountingEvent)
def auto_process_event(sender, instance, created, **kwargs):
    if created:
        process_event(instance)

@receiver(post_save, sender=Settlement)
def create_voucher_for_settlement(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.voucher_id:
        return

    v = post_settlement(instance)
    instance.voucher = v
    instance.save(update_fields=["voucher"])

