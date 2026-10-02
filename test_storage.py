"""Tests for restoring the SQLite database from OSS."""
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import db
import storage


class RestoreDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.temp_dir.name, "twos.sqlite")
        self.backup_path = os.path.join(self.temp_dir.name, "backup.sqlite")
        conn = sqlite3.connect(self.backup_path)
        try:
            conn.executescript(db.SCHEMA)
            conn.execute(
                "INSERT INTO items (content_text, created_at, updated_at) VALUES (?, ?, ?)",
                ("preserved from backup", "2026-10-03T00:00:00", "2026-10-03T00:00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _download_backup(self, key, destination):
        shutil.copyfile(self.backup_path, destination)
        return True

    def test_restores_backup_when_local_database_is_missing(self):
        with (
            patch.object(db, "DB_PATH", self.database_path),
            patch.object(storage, "oss_configured", return_value=True),
            patch.object(storage, "object_last_modified", return_value=datetime.now(timezone.utc)),
            patch.object(storage, "download_to", side_effect=self._download_backup),
            patch.object(storage, "backup_db_now") as backup,
        ):
            storage.restore_db_if_needed()

        conn = sqlite3.connect(self.database_path)
        try:
            text = conn.execute("SELECT content_text FROM items").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(text, "preserved from backup")
        backup.assert_not_called()

    def test_rejects_invalid_backup_without_creating_empty_database(self):
        with open(self.backup_path, "wb") as file:
            file.write(b"not a sqlite database")

        with (
            patch.object(db, "DB_PATH", self.database_path),
            patch.object(storage, "oss_configured", return_value=True),
            patch.object(storage, "object_last_modified", return_value=datetime.now(timezone.utc)),
            patch.object(storage, "download_to", side_effect=self._download_backup),
            self.assertRaises(RuntimeError),
        ):
            storage.restore_db_if_needed()

        self.assertFalse(os.path.exists(self.database_path))


class PersistDatabaseChangeTests(unittest.TestCase):
    def test_uploads_a_consistent_sqlite_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "current.sqlite")
            conn = sqlite3.connect(database_path)
            try:
                conn.executescript(db.SCHEMA)
                conn.execute(
                    "INSERT INTO items (content_text, created_at, updated_at) VALUES (?, ?, ?)",
                    ("saved before response", "2026-10-03T00:00:00", "2026-10-03T00:00:00"),
                )
                conn.commit()
            finally:
                conn.close()

            uploaded = {}

            def inspect_upload(key, path, content_type):
                conn = sqlite3.connect(path)
                try:
                    uploaded["text"] = conn.execute(
                        "SELECT content_text FROM items"
                    ).fetchone()[0]
                finally:
                    conn.close()
                uploaded["key"] = key
                uploaded["content_type"] = content_type
                return "oss://test"

            with (
                patch.object(db, "DB_PATH", database_path),
                patch.object(storage, "oss_configured", return_value=True),
                patch.object(storage, "upload_file", side_effect=inspect_upload),
            ):
                self.assertTrue(storage.backup_db_now())

            self.assertEqual(uploaded["text"], "saved before response")
            self.assertEqual(uploaded["key"], storage.OSS_DB_BACKUP_KEY)
            self.assertEqual(uploaded["content_type"], "application/x-sqlite3")

    def test_skips_backup_when_oss_is_not_configured(self):
        with (
            patch.object(storage, "oss_configured", return_value=False),
            patch.object(storage, "backup_db_now") as backup,
        ):
            result = storage.persist_db_change()

        self.assertIsNone(result)
        backup.assert_not_called()

    def test_returns_true_when_synchronous_backup_succeeds(self):
        with (
            patch.object(storage, "oss_configured", return_value=True),
            patch.object(storage, "backup_db_now", return_value=True),
        ):
            result = storage.persist_db_change()

        self.assertTrue(result)

    def test_queues_retry_when_synchronous_backup_fails(self):
        with (
            patch.object(storage, "oss_configured", return_value=True),
            patch.object(storage, "backup_db_now", return_value=False),
            patch.object(storage, "mark_dirty") as mark_dirty,
        ):
            result = storage.persist_db_change()

        self.assertFalse(result)
        mark_dirty.assert_called_once()


if __name__ == "__main__":
    unittest.main()
