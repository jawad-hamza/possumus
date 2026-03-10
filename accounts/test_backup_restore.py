from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import BackupSnapshot
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
            with override_settings(BASE_DIR=Path(tmpdir)):
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
            with override_settings(BASE_DIR=Path(tmpdir)):
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
            with override_settings(BASE_DIR=Path(tmpdir)):
                snapshot = create_backup_snapshot(label="Nightly", actor=self.admin_user)
                backup_path = get_backup_file_path(snapshot)

                self.assertTrue(backup_path.exists())
                self.assertTrue(snapshot.file_name.endswith(".sql"))

        create_backup_mock.assert_called_once()

    def test_download_backup_snapshot_returns_attachment(self):
        with TemporaryDirectory() as tmpdir:
            with override_settings(BASE_DIR=Path(tmpdir)):
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


