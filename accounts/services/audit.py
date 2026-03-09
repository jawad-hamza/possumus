from accounts.models import AuditLog


def record_audit(*, action: str, description: str, actor=None, target=None, metadata=None):
    metadata = metadata or {}
    target_model = ""
    target_id = ""

    if target is not None:
        target_model = target.__class__.__name__
        target_id = str(getattr(target, "pk", "") or "")

    return AuditLog.objects.create(
        action=action,
        actor=actor,
        target_model=target_model,
        target_id=target_id,
        description=description,
        metadata=metadata,
    )
