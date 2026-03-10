import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from django.conf import settings
from django.db import connections
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import BackupSnapshot
from accounts.services.audit import record_audit


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
    folder = Path(settings.BASE_DIR) / "backups"
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
    safe_label = slugify(label or "database-backup") or "database-backup"
    return f"{timestamp:%Y%m%d-%H%M%S}-{vendor}-{safe_label}{extension}"


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
    return {
        "vendor": vendor,
        "vendor_label": database_vendor_label(vendor),
        "database_name": _database_name(settings_dict),
        "download_supported": True,
        "allowed_upload_extensions": sorted(ALLOWED_UPLOAD_EXTENSIONS),
    }


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


def _write_uploaded_file(uploaded_file, target_path: Path):
    writer = getattr(uploaded_file, "chunks", None)
    with target_path.open("wb") as destination:
        if callable(writer):
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        else:
            destination.write(uploaded_file.read())


def upload_backup_snapshot(*, uploaded_file, label: str = "", notes: str = "", actor=None) -> BackupSnapshot:
    if uploaded_file is None:
        raise ValueError("Choose a backup snapshot file to upload.")

    original_name = Path(getattr(uploaded_file, "name", "")).name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError("Uploaded backups must use .sqlite3, .dump, or .sql files.")

    vendor = infer_backup_vendor(original_name)
    timestamp = timezone.now()
    label_text = label.strip() or Path(original_name).stem.replace("-", " ").replace("_", " ").title() or "Uploaded Backup"
    file_name = _snapshot_file_name(vendor=vendor, extension=extension, label=label_text, timestamp=timestamp)
    target_path = _backup_dir() / file_name
    _write_uploaded_file(uploaded_file, target_path)

    if not target_path.exists() or target_path.stat().st_size == 0:
        raise ValueError("The uploaded backup file was empty or could not be saved.")

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


def restore_backup_snapshot(*, snapshot: BackupSnapshot, actor=None) -> BackupSnapshot:
    source_path = get_backup_file_path(snapshot)
    vendor = _database_vendor()
    snapshot_vendor = infer_backup_vendor(snapshot.file_name)
    if snapshot_vendor != vendor:
        raise ValueError(
            f"This server is using {database_vendor_label(vendor)} but the selected backup was created for {database_vendor_label(snapshot_vendor)}."
        )

    settings_dict = _database_settings()
    connections.close_all()

    restorers = {
        "sqlite": _restore_sqlite_backup,
        "postgresql": _restore_postgresql_backup,
        "mysql": _restore_mysql_backup,
    }
    restorers[vendor](source_path, settings_dict)

    snapshot.restored_at = timezone.now()
    snapshot.save(update_fields=["restored_at"])
    record_audit(
        action="backup_restored",
        description=f"Backup snapshot '{snapshot.label}' was restored to the live database.",
        actor=actor,
        target=snapshot,
        metadata={
            "database_vendor": vendor,
            "file_name": snapshot.file_name,
        },
    )
    return snapshot
