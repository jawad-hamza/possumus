import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DatabaseError, connections
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import AuditLog, BackupSnapshot
from accounts.services.audit import record_audit

logger = logging.getLogger(__name__)

# A snapshot is the database only. Uploaded files (the shop logo and the bank receipts under
# MEDIA_ROOT/bank_receipts, or in the S3 bucket) are not inside it: the BankReceipt rows (who,
# when, name, sha256, stored path) travel with the snapshot, the files stay where they are and a
# restore never deletes them. A receipt row whose file is gone says so on its receipt page.


VENDOR_LABELS = {
    "sqlite": "SQLite",
    "postgresql": "PostgreSQL",
    "mysql": "MySQL",
}

BACKUP_EXTENSIONS = {
    "sqlite": ".sqlite3",
    "postgresql": ".dump",
    "mysql": ".sql",
}

ALLOWED_UPLOAD_EXTENSIONS = set(BACKUP_EXTENSIONS.values())

SQLITE_HEADER = b"SQLite format 3\x00"
# Uploaded files carry this marker in their server-chosen name so they can never be
# mistaken for a dump this server produced itself.
UPLOAD_MARKER = "upload"
_UPLOADED_FILE_NAME = re.compile(rf"^\d{{8}}-\d{{6}}-[a-z]+-{UPLOAD_MARKER}-")
SAFETY_BACKUP_LABEL = "Safety backup before restore"
DEFAULT_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


def _cloud_mode():
    return str(getattr(settings, "DEPLOYMENT_MODE", "cloud")).strip().lower() == "cloud"


def backups_available():
    # Serverless hosts have neither the dump binaries nor a disk that survives the request.
    return bool(getattr(settings, "BACKUPS_AVAILABLE", True))


def restore_supported():
    """Hosted deployments have no pg_restore/mysql binaries and no business swapping a shared database in-app."""
    if not backups_available():
        return False
    override = getattr(settings, "ALLOW_BACKUP_RESTORE", None)
    if override is not None:
        return bool(override)
    return not _cloud_mode()


def sql_restore_allowed():
    # pg_restore / mysql execute whatever the file contains, so an uploaded dump is
    # remote code execution against the database unless the operator opts in.
    return bool(getattr(settings, "ALLOW_SQL_RESTORE", False))


def allowed_upload_extensions(vendor=None):
    if not restore_supported():
        return []
    vendor = vendor or _database_vendor()
    if vendor == "sqlite":
        return [BACKUP_EXTENSIONS["sqlite"]]
    return [BACKUP_EXTENSIONS[vendor]] if sql_restore_allowed() else []


def _database_settings():
    return connections["default"].settings_dict


def _database_vendor():
    engine = (_database_settings().get("ENGINE") or "").lower()
    if "sqlite" in engine:
        return "sqlite"
    if "postgresql" in engine or "postgres" in engine:
        return "postgresql"
    if "mysql" in engine:
        return "mysql"
    raise ValueError("Unsupported database engine configured for backup/restore.")


def database_vendor_label(vendor=None):
    return VENDOR_LABELS.get(vendor or _database_vendor(), "Database")


def infer_backup_vendor(file_name):
    suffix = Path(file_name).suffix.lower()
    if suffix == ".sqlite3":
        return "sqlite"
    if suffix == ".dump":
        return "postgresql"
    if suffix == ".sql":
        return "mysql"
    return _database_vendor()


def _database_name(settings_dict=None):
    settings_dict = settings_dict or _database_settings()
    name = settings_dict.get("NAME") or ""
    if _database_vendor() == "sqlite":
        return Path(str(name)).name or str(name)
    return str(name)


def _is_sqlite_memory_database(name):
    name = str(name or "")
    return name == ":memory:" or (name.startswith("file:") and "mode=memory" in name)


def _backup_dir() -> Path:
    # An installed copy keeps its data outside the (read-only) program folder.
    folder = Path(getattr(settings, "BACKUP_DIR", None) or Path(settings.BASE_DIR) / "backups")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _resolve_snapshot_path(file_name: str) -> Path:
    backup_dir = _backup_dir().resolve()
    path = (backup_dir / file_name).resolve()
    if backup_dir != path.parent:
        raise ValueError("Backup file path is invalid.")
    return path


def _snapshot_file_name(*, vendor: str, extension: str, label: str, timestamp=None) -> str:
    timestamp = timestamp or timezone.now()
    safe_label = (slugify(label or "database-backup") or "database-backup")[:120]
    file_name = f"{timestamp:%Y%m%d-%H%M%S}-{vendor}-{safe_label}{extension}"
    if (_backup_dir() / file_name).exists():
        # Two snapshots with one label inside the same second must not overwrite each other.
        file_name = f"{timestamp:%Y%m%d-%H%M%S}-{vendor}-{safe_label}-{secrets.token_hex(3)}{extension}"
    return file_name


def _create_snapshot_record(*, file_name: str, label: str, notes: str, size_bytes: int, actor, action: str, description: str, metadata=None):
    snapshot = BackupSnapshot.objects.create(
        label=label.strip() or f"Backup {timezone.now():%d %b %Y %H:%M}",
        file_name=file_name,
        notes=notes,
        size_bytes=size_bytes,
        created_by=actor,
    )
    record_audit(
        action=action,
        description=description.format(label=snapshot.label),
        actor=actor,
        target=snapshot,
        metadata=metadata or {},
    )
    return snapshot


def get_backup_file_path(snapshot: BackupSnapshot) -> Path:
    path = _resolve_snapshot_path(snapshot.file_name)
    if not path.exists():
        raise ValueError("The selected backup file is missing from disk.")
    return path


def backup_runtime_summary():
    vendor = _database_vendor()
    settings_dict = _database_settings()
    upload_extensions = allowed_upload_extensions(vendor)
    return {
        "vendor": vendor,
        "vendor_label": database_vendor_label(vendor),
        "database_name": _database_name(settings_dict),
        "download_supported": True,
        "backups_available": backups_available(),
        "restore_supported": restore_supported(),
        "upload_supported": bool(upload_extensions),
        "allowed_upload_extensions": upload_extensions,
    }


def snapshot_was_uploaded(snapshot: BackupSnapshot) -> bool:
    if _UPLOADED_FILE_NAME.match(snapshot.file_name or ""):
        return True
    # Uploads made before the file-name marker existed are only known to the audit trail.
    try:
        return AuditLog.objects.filter(action="backup_uploaded", metadata__file_name=snapshot.file_name).exists()
    except DatabaseError:
        return True


def snapshot_restore_block_reason(snapshot: BackupSnapshot) -> str:
    """Empty string when this snapshot may be restored here, otherwise the reason shown to the admin."""
    if not restore_supported():
        return "Restore is turned off on this hosted deployment. Download the snapshot and contact support to recover data."
    vendor = _database_vendor()
    snapshot_vendor = infer_backup_vendor(snapshot.file_name)
    if snapshot_vendor != vendor:
        return (
            f"This server is using {database_vendor_label(vendor)} but the selected backup was created for "
            f"{database_vendor_label(snapshot_vendor)}."
        )
    if vendor != "sqlite" and snapshot_was_uploaded(snapshot) and not sql_restore_allowed():
        return "Uploaded SQL dumps cannot be restored on this server. Only snapshots created here can be restored."
    return ""


def validate_sqlite_snapshot(path: Path):
    """Refuse anything that is not an intact Django SQLite database before it can replace the live one."""
    try:
        with path.open("rb") as handle:
            header = handle.read(len(SQLITE_HEADER))
    except OSError as exc:
        raise ValueError("The backup file could not be read.") from exc
    if header != SQLITE_HEADER:
        raise ValueError("That file is not a SQLite database backup.")

    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError("The backup file could not be opened as a SQLite database.") from exc
    try:
        result = connection.execute("PRAGMA integrity_check").fetchall()
        if [row[0] for row in result] != ["ok"]:
            raise ValueError("The backup file is damaged (SQLite integrity check failed).")
        has_migrations = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'django_migrations'"
        ).fetchone()
        if not has_migrations:
            raise ValueError("That SQLite file is not a backup of this application.")
    except sqlite3.Error as exc:
        raise ValueError("The backup file is damaged or is not a SQLite database.") from exc
    finally:
        connection.close()


def _command_path(command_name: str) -> str:
    command = shutil.which(command_name)
    if not command:
        raise ValueError(f"'{command_name}' is not installed on the server, so this backup action cannot run.")
    return command


def _run_command(command, *, env_overrides=None, stdin=None):
    env = os.environ.copy()
    if env_overrides:
        env.update({key: value for key, value in env_overrides.items() if value not in (None, "")})

    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            env=env,
            stdin=stdin,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        detail = (stderr or stdout or "The database command failed.").strip()
        raise ValueError(detail) from exc
    return completed


def _postgresql_env(settings_dict):
    env_map = {}
    if settings_dict.get("PASSWORD"):
        env_map["PGPASSWORD"] = settings_dict["PASSWORD"]
    sslmode = (settings_dict.get("OPTIONS") or {}).get("sslmode")
    if sslmode:
        env_map["PGSSLMODE"] = sslmode
    return env_map


def _mysql_env(settings_dict):
    if settings_dict.get("PASSWORD"):
        return {"MYSQL_PWD": settings_dict["PASSWORD"]}
    return {}


def _append_network_options(command, settings_dict, *, supports_socket=False):
    host = settings_dict.get("HOST") or ""
    port = settings_dict.get("PORT") or ""
    user = settings_dict.get("USER") or ""

    if host:
        if supports_socket and (host.startswith("/") or host.endswith(".sock")):
            command.extend(["--socket", host])
        else:
            command.extend(["--host", host])
    if port:
        command.extend(["--port", str(port)])
    if user:
        command.extend(["--username", user])
    return command


def _create_sqlite_backup(target_path: Path, _settings_dict):
    connection = connections["default"]
    connection.ensure_connection()
    source_db = connection.connection
    if source_db is None:
        raise ValueError("SQLite connection is not available for backup.")

    with sqlite3.connect(str(target_path)) as backup_db:
        source_db.backup(backup_db)


def _restore_sqlite_backup(source_path: Path, settings_dict):
    connection = connections["default"]
    connection.close()

    database_name = settings_dict.get("NAME")
    if _is_sqlite_memory_database(database_name):
        connection.ensure_connection()
        live_db = connection.connection
        if live_db is None:
            raise ValueError("SQLite connection is not available for restore.")
        with sqlite3.connect(str(source_path)) as backup_db:
            backup_db.backup(live_db)
        return

    live_db_path = Path(str(database_name))
    live_db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(source_path)) as backup_db, sqlite3.connect(str(live_db_path)) as live_db:
        backup_db.backup(live_db)


def _create_postgresql_backup(target_path: Path, settings_dict):
    command = [_command_path("pg_dump")]
    _append_network_options(command, settings_dict)
    command.extend(
        [
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(target_path),
            str(settings_dict.get("NAME") or ""),
        ]
    )
    _run_command(command, env_overrides=_postgresql_env(settings_dict))


def _restore_postgresql_backup(source_path: Path, settings_dict):
    command = [_command_path("pg_restore")]
    _append_network_options(command, settings_dict)
    command.extend(
        [
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            str(settings_dict.get("NAME") or ""),
            str(source_path),
        ]
    )
    _run_command(command, env_overrides=_postgresql_env(settings_dict))


def _create_mysql_backup(target_path: Path, settings_dict):
    command = [_command_path("mysqldump")]
    _append_network_options(command, settings_dict, supports_socket=True)
    command.extend(
        [
            "--default-character-set=utf8mb4",
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--result-file",
            str(target_path),
            str(settings_dict.get("NAME") or ""),
        ]
    )
    _run_command(command, env_overrides=_mysql_env(settings_dict))


def _restore_mysql_backup(source_path: Path, settings_dict):
    command = [_command_path("mysql")]
    _append_network_options(command, settings_dict, supports_socket=True)
    command.extend(["--default-character-set=utf8mb4", str(settings_dict.get("NAME") or "")])
    with source_path.open("rb") as backup_stream:
        _run_command(command, env_overrides=_mysql_env(settings_dict), stdin=backup_stream)


def _write_uploaded_file(uploaded_file, target_path: Path, *, max_bytes: int):
    writer = getattr(uploaded_file, "chunks", None)
    written = 0
    with target_path.open("wb") as destination:
        chunks = uploaded_file.chunks() if callable(writer) else [uploaded_file.read()]
        for chunk in chunks:
            written += len(chunk)
            if written > max_bytes:
                raise ValueError("The uploaded backup is larger than this server accepts.")
            destination.write(chunk)


def upload_backup_snapshot(*, uploaded_file, label: str = "", notes: str = "", actor=None) -> BackupSnapshot:
    if uploaded_file is None:
        raise ValueError("Choose a backup snapshot file to upload.")

    if not restore_supported():
        raise ValueError("Uploading backups is turned off on this hosted deployment.")

    original_name = Path(getattr(uploaded_file, "name", "")).name
    extension = Path(original_name).suffix.lower()
    accepted = allowed_upload_extensions()
    if not accepted:
        raise ValueError("Uploading database dumps is turned off on this server.")
    if extension not in accepted:
        raise ValueError(f"Uploaded backups for this server must be {' or '.join(accepted)} files.")

    vendor = infer_backup_vendor(original_name)
    timestamp = timezone.now()
    label_text = label.strip() or Path(original_name).stem.replace("-", " ").replace("_", " ").title() or "Uploaded Backup"
    file_name = _snapshot_file_name(
        vendor=vendor,
        extension=extension,
        label=f"{UPLOAD_MARKER}-{slugify(label_text) or 'backup'}",
        timestamp=timestamp,
    )
    target_path = _backup_dir() / file_name
    max_bytes = int(getattr(settings, "MAX_BACKUP_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES))
    try:
        _write_uploaded_file(uploaded_file, target_path, max_bytes=max_bytes)
        if not target_path.exists() or target_path.stat().st_size == 0:
            raise ValueError("The uploaded backup file was empty or could not be saved.")
        if vendor == "sqlite":
            validate_sqlite_snapshot(target_path)
    except ValueError:
        target_path.unlink(missing_ok=True)
        raise

    return _create_snapshot_record(
        file_name=file_name,
        label=label_text,
        notes=notes,
        size_bytes=target_path.stat().st_size,
        actor=actor,
        action="backup_uploaded",
        description="Backup snapshot '{label}' uploaded.",
        metadata={
            "database_vendor": vendor,
            "file_name": file_name,
            "original_name": original_name,
            "size_bytes": target_path.stat().st_size,
        },
    )


def create_backup_snapshot(*, label: str = "", notes: str = "", actor=None) -> BackupSnapshot:
    if not backups_available():
        raise ValueError("Backups are handled by the hosting provider on this deployment, not inside the app.")
    vendor = _database_vendor()
    settings_dict = _database_settings()
    timestamp = timezone.now()
    label_text = label.strip() or f"Backup {timestamp:%d %b %Y %H:%M}"
    file_name = _snapshot_file_name(vendor=vendor, extension=BACKUP_EXTENSIONS[vendor], label=label_text, timestamp=timestamp)
    target_path = _backup_dir() / file_name

    creators = {
        "sqlite": _create_sqlite_backup,
        "postgresql": _create_postgresql_backup,
        "mysql": _create_mysql_backup,
    }
    creators[vendor](target_path, settings_dict)

    if not target_path.exists():
        raise ValueError("The backup command completed without creating a backup file.")

    return _create_snapshot_record(
        file_name=file_name,
        label=label_text,
        notes=notes,
        size_bytes=target_path.stat().st_size,
        actor=actor,
        action="backup_created",
        description="Backup snapshot '{label}' created.",
        metadata={
            "database_vendor": vendor,
            "file_name": file_name,
            "size_bytes": target_path.stat().st_size,
        },
    )


def _migrate_restored_database():
    # A backup taken by an older version of the app has an older schema.
    call_command("migrate", interactive=False, verbosity=0)


def _existing_user(actor):
    if actor is None or not getattr(actor, "pk", None):
        return None
    return get_user_model().objects.filter(pk=actor.pk, username=actor.get_username()).first()


def _snapshot_fields(snapshot: BackupSnapshot) -> dict:
    return {
        "label": snapshot.label,
        "file_name": snapshot.file_name,
        "notes": snapshot.notes,
        "size_bytes": snapshot.size_bytes,
    }


def _upsert_snapshot_row(fields: dict, *, actor, restored_at=None) -> BackupSnapshot:
    """The restored database predates these rows, so they are written back by file name."""
    defaults = {"label": fields["label"], "notes": fields["notes"], "size_bytes": fields["size_bytes"]}
    if restored_at is not None:
        defaults["restored_at"] = restored_at
    snapshot, created = BackupSnapshot.objects.update_or_create(file_name=fields["file_name"], defaults=defaults)
    if created and actor is not None:
        snapshot.created_by = actor
        snapshot.save(update_fields=["created_by"])
    return snapshot


def restore_backup_snapshot(*, snapshot: BackupSnapshot, actor=None) -> BackupSnapshot:
    block_reason = snapshot_restore_block_reason(snapshot)
    if block_reason:
        raise ValueError(block_reason)

    source_path = get_backup_file_path(snapshot)
    vendor = _database_vendor()
    if vendor == "sqlite":
        validate_sqlite_snapshot(source_path)

    # Taken before anything is touched: if the restore turns out to be a mistake, this
    # snapshot is the way back.
    safety_snapshot = create_backup_snapshot(
        label=SAFETY_BACKUP_LABEL,
        notes=f"Taken automatically before restoring '{snapshot.label}'.",
        actor=actor,
    )
    restored_fields = _snapshot_fields(snapshot)
    safety_fields = _snapshot_fields(safety_snapshot)

    settings_dict = _database_settings()
    connections.close_all()

    restorers = {
        "sqlite": _restore_sqlite_backup,
        "postgresql": _restore_postgresql_backup,
        "mysql": _restore_mysql_backup,
    }
    restorers[vendor](source_path, settings_dict)

    follow_up_problems = []
    try:
        _migrate_restored_database()
    except Exception:
        logger.exception("Migrating the restored database failed.")
        follow_up_problems.append("The restored data could not be upgraded to this version of the app automatically.")

    try:
        restored_actor = _existing_user(actor)
        _upsert_snapshot_row(safety_fields, actor=restored_actor)
        snapshot = _upsert_snapshot_row(restored_fields, actor=restored_actor, restored_at=timezone.now())
        record_audit(
            action="backup_restored",
            description=f"Backup snapshot '{snapshot.label}' was restored to the live database.",
            actor=restored_actor,
            target=snapshot,
            metadata={
                "database_vendor": vendor,
                "file_name": snapshot.file_name,
                "safety_backup_file": safety_fields["file_name"],
                "restored_by": actor.get_username() if actor is not None else "",
            },
        )
    except DatabaseError:
        logger.exception("Recording the restore in the restored database failed.")
        follow_up_problems.append("The restore finished, but it could not be written to the backup history.")

    snapshot.safety_backup_file = safety_fields["file_name"]
    snapshot.follow_up_problems = follow_up_problems
    return snapshot
