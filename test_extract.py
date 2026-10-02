"""Tests for attachment text extraction and search-index migrations."""
import os
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import db
import extract


class TextExtractionTests(unittest.TestCase):
    def test_decodes_chinese_gb18030_text_files(self):
        text = "采购清单：打印机"
        self.assertIn(text, extract.extract_text(text.encode("gb18030"), "notes.txt"))

    def test_decodes_utf16_text_files(self):
        text = "会议安排"
        self.assertIn(text, extract.extract_text(text.encode("utf-16"), "notes.txt"))

    def test_extracts_inline_and_shared_strings_from_xlsx(self):
        with tempfile.SpooledTemporaryFile() as file:
            with zipfile.ZipFile(file, "w") as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    '<sst><si><t>共享关键词</t></si></sst>',
                )
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    '<worksheet><sheetData><row>'
                    '<c t="inlineStr"><is><t>内嵌关键词</t></is></c>'
                    '<c t="s"><v>0</v></c>'
                    '</row></sheetData></worksheet>',
                )
            file.seek(0)
            text = extract.extract_text(file.read(), "sheet.xlsx")

        self.assertIn("内嵌关键词", text)
        self.assertIn("共享关键词", text)

    def test_marks_common_text_formats_as_searchable(self):
        for filename in ("notes.toml", "data.tsv", "source.ts", "script.ps1"):
            with self.subTest(filename=filename):
                self.assertTrue(extract.supports_extraction(filename))

    def test_migrates_legacy_attachment_table_to_versioned_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = os.path.join(temp_dir, "legacy.sqlite")
            legacy_schema = db.SCHEMA.replace(
                "    extracted_text TEXT DEFAULT '',\n"
                "    extraction_version INTEGER DEFAULT 0\n",
                "    extracted_text TEXT DEFAULT ''\n",
            )
            conn = sqlite3.connect(database_path)
            try:
                conn.executescript(legacy_schema)
                conn.execute(
                    """INSERT INTO attachments
                       (filename, oss_key, url, created_at, extracted_text)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("notes.txt", "notes.txt", "/att/1", "2026-10-03", "old index"),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(db, "DB_PATH", database_path):
                db.init_db()

            conn = sqlite3.connect(database_path)
            try:
                version = conn.execute(
                    "SELECT extraction_version FROM attachments"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(version, 0)


if __name__ == "__main__":
    unittest.main()
