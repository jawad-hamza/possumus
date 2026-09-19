import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import AuditLog, BackupSnapshot
from accounts.services import backup_restore
from accounts.services.backup_restore import create_backup_snapshot, get_backup_file_path
from pos.test_support import AuthenticatedAdminTestCase


class BackupServiceTests(AuthenticatedAdminTestCase):
    @mock.patch("accounts.services.backup_restore._database_settings")
    @mock.patch("accounts.services.backup_restore._database_vendor", return_value="sqlite")
    @mock.patch("accounts.services.backup_restore._create_sqlite_backup")
    def test_sqlite_backup_uses_sqlite_helper(self, create_backup_mock, _vendor_mock, settings_mock):
        settings_mock.return_value = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "db.sqlite3",
        }

        def fake_backup(target_path, _settings_dict):
            target_path.write_bytes(b"sqlite backup")

        create_backup_mock.side_effect = fake_backup

        with TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir), BACKUP_DIR=Path(tmpdir) / "backups"):
                snapshot = create_backup_snapshot(label="Before release", actor=self.admin_user)
                backup_path = get_backup_file_path(snapshot)

                self.assertTrue(backup_path.exists())
                self.assertTrue(snapshot.file_name.endswith(".sqlite3"))

        create_backup_mock.assert_called_once()

    @mock.patch("accounts.services.backup_restore._database_settings")
    @mock.patch("accounts.services.backup_restore._database_vendor", return_value="postgresql")
    @mock.patch("accounts.services.backup_restore._create_postgresql_backup")
    def test_postgresql_backup_uses_pg_dump_helper(self, create_backup_mock, _vendor_mock, settings_mock):
        settings_mock.return_value = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "possumus",
            "USER": "postgres",
            "PASSWORD": "secret",
            "HOST": "127.0.0.1",
            "PORT": "5432",
            "OPTIONS": {"sslmode": "require"},
        }

        def fake_backup(target_path, _settings_dict):
            target_path.write_bytes(b"postgres backup")

        create_backup_mock.side_effect = fake_backup

        with TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir), BACKUP_DIR=Path(tmpdir) / "backups"):
                snapshot = create_backup_snapshot(label="Nightly", actor=self.admin_user)
                backup_path = get_backup_file_path(snapshot)

                self.assertTrue(backup_path.exists())
                self.assertTrue(snapshot.file_name.endswith(".dump"))

        create_backup_mock.assert_called_once()

    @mock.patch("accounts.services.backup_restore._database_settings")
    @mock.patch("accounts.services.backup_restore._database_vendor", return_value="mysql")
    @mock.patch("accounts.services.backup_restore._create_mysql_backup")
    def test_mysql_backup_uses_mysqldump_helper(self, create_backup_mock, _vendor_mock, settings_mock):
        settings_mock.return_value = {
            "ENGINE": "django.db.backends.mysql",
            "NAME": "possumus",
            "USER": "root",
            "PASSWORD": "secret",
            "HOST": "127.0.0.1",
            "PORT": "3306",
        }

        def fake_backup(target_path, _settings_dict):
            target_path.write_bytes(b"mysql backup")

        create_backup_mock.side_effect = fake_backup

        with TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir), BACKUP_DIR=Path(tmpdir) / "backups"):
                snapshot = create_backup_snapshot(label="Nightly", actor=self.admin_user)
                backup_path = get_backup_file_path(snapshot)

                self.assertTrue(backup_path.exists())
                self.assertTrue(snapshot.file_name.endswith(".sql"))

        create_backup_mock.assert_called_once()

    def test_download_backup_snapshot_returns_attachment(self):
        with TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir), BACKUP_DIR=Path(tmpdir) / "backups"):
                backup_dir = Path(tmpdir) / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                file_name = "20260310-120000-sqlite-before-release.sqlite3"
                (backup_dir / file_name).write_bytes(b"sqlite backup")
                snapshot = BackupSnapshot.objects.create(
                    label="Before release",
                    file_name=file_name,
                    size_bytes=13,
                    created_by=self.admin_user,
                )

                response = self.client.get(reverse("download_backup_snapshot", args=[snapshot.id]))

                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment;", response["Content-Disposition"])
                self.assertIn(file_name, response["Content-Disposition"])
                response.close()


class BackupCommandTests(TestCase):
    def test_postgresql_restore_command_uses_pg_restore(self):
        settings_dict = {
            "NAME": "possumus",
            "USER": "postgres",
            "PASSWORD": "secret",
            "HOST": "127.0.0.1",
            "PORT": "5432",
            "OPTIONS": {"sslmode": "require"},
        }

        with TemporaryDirectory() as tmpdir:
            source_path = Path(tmpdir) / "backup.dump"
            source_path.write_bytes(b"data")
            with mock.patch("accounts.services.backup_restore._command_path", return_value="pg_restore"):
                with mock.patch("accounts.services.backup_restore._run_command") as run_command_mock:
                    backup_restore._restore_postgresql_backup(source_path, settings_dict)

        command = run_command_mock.call_args.args[0]
        self.assertEqual(command[0], "pg_restore")
        self.assertIn("--dbname", command)
        self.assertIn(str(source_path), command)


def _sqlite_backup_bytes(*, with_migrations_table=True) -> bytes:
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "source.sqlite3"
        connection = sqlite3.connect(str(db_path))
        if with_migrations_table:
            connection.execute("CREATE TABLE django_migrations (id integer primary key, app text, name text)")
        else:
            connection.execute("CREATE TABLE something_else (id integer primary key)")
        connection.commit()
        connection.close()
        return db_path.read_bytes()


class SqliteSnapshotValidationTests(TestCase):
    def _validate(self, payload: bytes):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidate.sqlite3"
            path.write_bytes(payload)
            backup_restore.validate_sqlite_snapshot(path)

    def test_accepts_an_intact_django_database(self):
        self._validate(_sqlite_backup_bytes())

    def test_rejects_a_file_that_is_not_sqlite(self):
        with self.assertRaisesMessage(ValueError, "not a SQLite database"):
            self._validate(b"DROP TABLE users; -- renamed to .sqlite3")

    def test_rejects_sqlite_files_from_another_application(self):
        with self.assertRaisesMessage(ValueError, "not a backup of this application"):
            self._validate(_sqlite_backup_bytes(with_migrations_table=False))

    def test_rejects_a_truncated_database(self):
        payload = _sqlite_backup_bytes()
        with self.assertRaises(ValueError):
            self._validate(payload[: len(payload) // 2])


@override_settings(DEPLOYMENT_MODE="desktop")
class RestoreSafetyTests(AuthenticatedAdminTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self._tmpdir = TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        base_dir = override_settings(BASE_DIR=Path(self._tmpdir.name), BACKUP_DIR=Path(self._tmpdir.name) / "backups")
        base_dir.enable()
        self.addCleanup(base_dir.disable)
        self.backup_dir = Path(self._tmpdir.name) / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def _snapshot(self, file_name="20260310-120000-sqlite-before-release.sqlite3", payload=None, label="Before release"):
        payload = _sqlite_backup_bytes() if payload is None else payload
        (self.backup_dir / file_name).write_bytes(payload)
        return BackupSnapshot.objects.create(
            label=label, file_name=file_name, size_bytes=len(payload), created_by=self.admin_user
        )

    def _fake_live_backup(self, target_path, _settings_dict):
        target_path.write_bytes(_sqlite_backup_bytes())

    def _post_restore(self, snapshot, **extra):
        return self.client.post(
            reverse("settings_page"), {"action": "restore_backup", "snapshot_id": snapshot.id, **extra}
        )

    def test_upload_rejects_a_fake_sqlite_file_and_leaves_nothing_behind(self):
        upload = SimpleUploadedFile("evil.sqlite3", b"not a database at all")

        with self.assertRaises(ValueError):
            backup_restore.upload_backup_snapshot(uploaded_file=upload, label="Evil", actor=self.admin_user)

        self.assertEqual(list(self.backup_dir.iterdir()), [])
        self.assertFalse(BackupSnapshot.objects.exists())

    def test_upload_accepts_a_real_backup_and_marks_it_as_uploaded(self):
        upload = SimpleUploadedFile("shop.sqlite3", _sqlite_backup_bytes())

        snapshot = backup_restore.upload_backup_snapshot(uploaded_file=upload, label="From old PC", actor=self.admin_user)

        self.assertTrue(backup_restore.snapshot_was_uploaded(snapshot))
        self.assertTrue((self.backup_dir / snapshot.file_name).exists())

    def test_upload_refuses_sql_dumps_on_a_sqlite_install(self):
        upload = SimpleUploadedFile("dump.sql", b"select 1;")

        with self.assertRaisesMessage(ValueError, ".sqlite3"):
            backup_restore.upload_backup_snapshot(uploaded_file=upload, actor=self.admin_user)

    @mock.patch("accounts.services.backup_restore._migrate_restored_database")
    @mock.patch("accounts.services.backup_restore._restore_sqlite_backup")
    @mock.patch("accounts.services.backup_restore._create_sqlite_backup")
    def test_restore_takes_a_safety_backup_first_and_rewrites_history(self, create_mock, restore_mock, migrate_mock):
        snapshot = self._snapshot()
        create_mock.side_effect = self._fake_live_backup
        events = []

        def fake_restore(_source_path, _settings_dict):
            events.append(("restore", BackupSnapshot.objects.filter(label=backup_restore.SAFETY_BACKUP_LABEL).exists()))
            # The restored database predates both snapshot rows and the audit trail.
            BackupSnapshot.objects.all().delete()

        restore_mock.side_effect = fake_restore

        restored = backup_restore.restore_backup_snapshot(snapshot=snapshot, actor=self.admin_user)

        self.assertEqual(events, [("restore", True)])
        migrate_mock.assert_called_once()
        self.assertEqual(restored.follow_up_problems, [])
        row = BackupSnapshot.objects.get(file_name=snapshot.file_name)
        self.assertIsNotNone(row.restored_at)
        self.assertEqual(row.label, "Before release")
        safety = BackupSnapshot.objects.get(label=backup_restore.SAFETY_BACKUP_LABEL)
        self.assertTrue((self.backup_dir / safety.file_name).exists())
        audit = AuditLog.objects.get(action="backup_restored")
        self.assertEqual(audit.target_id, str(row.pk))
        self.assertEqual(audit.metadata["safety_backup_file"], safety.file_name)

    @mock.patch("accounts.services.backup_restore._restore_sqlite_backup")
    @mock.patch("accounts.services.backup_restore._create_sqlite_backup")
    def test_restore_refuses_a_corrupt_file_before_touching_anything(self, create_mock, restore_mock):
        snapshot = self._snapshot(payload=b"SQLite format 3\x00 but then garbage")

        with self.assertRaises(ValueError):
            backup_restore.restore_backup_snapshot(snapshot=snapshot, actor=self.admin_user)

        create_mock.assert_not_called()
        restore_mock.assert_not_called()

    @mock.patch("accounts.services.backup_restore._restore_sqlite_backup")
    @mock.patch("accounts.services.backup_restore._create_sqlite_backup", side_effect=ValueError("disk full"))
    def test_restore_is_abandoned_when_the_safety_backup_fails(self, _create_mock, restore_mock):
        snapshot = self._snapshot()

        with self.assertRaisesMessage(ValueError, "disk full"):
            backup_restore.restore_backup_snapshot(snapshot=snapshot, actor=self.admin_user)

        restore_mock.assert_not_called()

    @mock.patch("pos.views.settings_views.restore_backup_snapshot")
    def test_restore_asks_for_the_password_before_doing_anything(self, restore_mock):
        snapshot = self._snapshot()

        response = self._post_restore(snapshot)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your password")
        self.assertContains(response, "Back Up Current Data and Restore")
        restore_mock.assert_not_called()

    @mock.patch("pos.views.settings_views.restore_backup_snapshot")
    def test_restore_with_a_wrong_password_is_refused_and_audited(self, restore_mock):
        snapshot = self._snapshot()

        response = self._post_restore(snapshot, confirm_password="not-the-password")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "That password is not correct")
        restore_mock.assert_not_called()
        self.assertTrue(AuditLog.objects.filter(action="backup_restore_denied", actor=self.admin_user).exists())

    @mock.patch("pos.views.settings_views.restore_backup_snapshot")
    def test_restore_password_guessing_is_throttled(self, restore_mock):
        snapshot = self._snapshot()
        for _ in range(5):
            self._post_restore(snapshot, confirm_password="not-the-password")

        response = self._post_restore(snapshot, confirm_password="AdminPass123!")

        self.assertEqual(response.status_code, 302)
        restore_mock.assert_not_called()

    @mock.patch("pos.views.settings_views.restore_backup_snapshot")
    def test_restore_with_the_right_password_runs_and_keeps_the_admin_signed_in(self, restore_mock):
        snapshot = self._snapshot()
        restore_mock.return_value = snapshot

        response = self._post_restore(snapshot, confirm_password="AdminPass123!")

        self.assertRedirects(response, reverse("settings_page"), fetch_redirect_response=False)
        restore_mock.assert_called_once()
        self.assertEqual(self.client.session["_auth_user_id"], str(self.admin_user.pk))
        self.assertEqual(self.client.get(reverse("settings_page")).status_code, 200)

    @mock.patch("pos.views.settings_views.restore_backup_snapshot", side_effect=DatabaseError("no such table"))
    def test_database_error_during_restore_becomes_a_message_not_a_500(self, _restore_mock):
        snapshot = self._snapshot()

        response = self._post_restore(snapshot, confirm_password="AdminPass123!")

        self.assertEqual(response.status_code, 302)
        follow = self.client.get(response.url)
        self.assertContains(follow, "could not be restored cleanly")

    @override_settings(DEPLOYMENT_MODE="cloud")
    @mock.patch("accounts.services.backup_restore._restore_sqlite_backup")
    def test_cloud_mode_has_no_restore_or_upload(self, restore_mock):
        snapshot = self._snapshot()

        self.assertFalse(backup_restore.backup_runtime_summary()["restore_supported"])
        self.assertEqual(backup_restore.backup_runtime_summary()["allowed_upload_extensions"], [])
        with self.assertRaises(ValueError):
            backup_restore.restore_backup_snapshot(snapshot=snapshot, actor=self.admin_user)
        with self.assertRaises(ValueError):
            backup_restore.upload_backup_snapshot(
                uploaded_file=SimpleUploadedFile("shop.sqlite3", _sqlite_backup_bytes()), actor=self.admin_user
            )

        response = self._post_restore(snapshot, confirm_password="AdminPass123!")
        self.assertEqual(response.status_code, 302)
        restore_mock.assert_not_called()

        page = self.client.get(reverse("settings_page"))
        self.assertTrue(all(not item.can_restore_here for item in page.context["backup_snapshots"]))

    @mock.patch("accounts.services.backup_restore._database_vendor", return_value="postgresql")
    def test_uploaded_sql_dumps_are_not_restorable_unless_enabled(self, _vendor_mock):
        uploaded = self._snapshot(file_name="20260310-120000-postgresql-upload-from-laptop.dump", payload=b"PGDMP")
        created_here = self._snapshot(file_name="20260310-130000-postgresql-nightly.dump", payload=b"PGDMP", label="Nightly")

        with override_settings(ALLOW_SQL_RESTORE=False):
            self.assertIn("Uploaded SQL dumps", backup_restore.snapshot_restore_block_reason(uploaded))
            self.assertEqual(backup_restore.snapshot_restore_block_reason(created_here), "")
            self.assertEqual(backup_restore.allowed_upload_extensions(), [])
        with override_settings(ALLOW_SQL_RESTORE=True):
            self.assertEqual(backup_restore.snapshot_restore_block_reason(uploaded), "")
            self.assertEqual(backup_restore.allowed_upload_extensions(), [".dump"])

    @mock.patch("accounts.services.backup_restore._database_vendor", return_value="postgresql")
    def test_legacy_uploads_are_recognised_through_the_audit_trail(self, _vendor_mock):
        legacy = self._snapshot(file_name="20250101-120000-postgresql-old-upload.dump", payload=b"PGDMP")
        AuditLog.objects.create(
            action="backup_uploaded", description="Uploaded", metadata={"file_name": legacy.file_name}
        )

        with override_settings(ALLOW_SQL_RESTORE=False):
            self.assertIn("Uploaded SQL dumps", backup_restore.snapshot_restore_block_reason(legacy))
