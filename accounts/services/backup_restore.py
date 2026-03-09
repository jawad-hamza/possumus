import sqlite3
from pathlib import Path

from django.conf import settings
from django.db import connections
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import BackupSnapshot
from accounts.services.audit import record_audit


def _db_path() -> Path:
    db_settings = settings.DATABASES["default"]
    engine = db_settings.get("ENGINE", "")
    if "sqlite" not in engine:
        raise ValueError("Backup and restore is currently supported only for SQLite deployments.")
    return Path(db_settings["NAME"])


def _backup_dir() -> Path:
    folder = Path(settings.BASE_DIR) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def create_backup_snapshot(*, label: str = "", notes: str = "", actor=None) -> BackupSnapshot:
    source_path = _db_path()
    if not source_path.exists():
        raise ValueError("Database file was not found.")

    timestamp = timezone.now()
    safe_label = slugify(label or "database-backup") or "database-backup"
    file_name = f"{timestamp:%Y%m%d-%H%M%S}-{safe_label}.sqlite3"
    target_path = _backup_dir() / file_name

    connections["default"].close()
    with sqlite3.connect(str(source_path)) as source_db, sqlite3.connect(str(target_path)) as backup_db:
        source_db.backup(backup_db)

    snapshot = BackupSnapshot.objects.create(
        label=label.strip() or f"Backup {timestamp:%d %b %Y %H:%M}",
        file_name=file_name,
        notes=notes,
        size_bytes=target_path.stat().st_size,
        created_by=actor,
    )
    record_audit(
        action="backup_created",
        description=f"Backup snapshot '{snapshot.label}' created.",
        actor=actor,
        target=snapshot,
        metadata={"file_name": snapshot.file_name, "size_bytes": snapshot.size_bytes},
    )
    return snapshot


def restore_backup_snapshot(*, snapshot: BackupSnapshot, actor=None) -> BackupSnapshot:
    source_path = _backup_dir() / snapshot.file_name
    if not source_path.exists():
        raise ValueError("The selected backup file is missing from disk.")

    live_db_path = _db_path()
    connections["default"].close()
    with sqlite3.connect(str(source_path)) as backup_db, sqlite3.connect(str(live_db_path)) as live_db:
        backup_db.backup(live_db)

    snapshot.restored_at = timezone.now()
    snapshot.save(update_fields=["restored_at"])
    record_audit(
        action="backup_restored",
        description=f"Backup snapshot '{snapshot.label}' was restored to the live database.",
        actor=actor,
        target=snapshot,
        metadata={"file_name": snapshot.file_name},
    )
    return snapshot
