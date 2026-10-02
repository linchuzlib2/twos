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


if __name__ == "__main__":
    unittest.main()
