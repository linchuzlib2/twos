# -*- coding: utf-8 -*-
"""数据库层：SQLite + 中国大陆时区 + HTML 清洗/纯文本提取"""
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "twos.sqlite")

TZ = ZoneInfo("Asia/Shanghai")  # 中国大陆时间（北京时间）

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    emoji TEXT DEFAULT '',
    sort INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    list_id INTEGER REFERENCES lists(id) ON DELETE CASCADE,
    date TEXT,
    content_html TEXT DEFAULT '',
    content_text TEXT DEFAULT '',
    done INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0,
    is_todo INTEGER DEFAULT 0,
    remind_at TEXT,
    notified_at TEXT,
    carried_from TEXT,
    sort INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);
CREATE INDEX IF NOT EXISTS idx_items_list ON items(list_id);
CREATE INDEX IF NOT EXISTS idx_items_remind ON items(remind_at);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    oss_key TEXT NOT NULL,
    url TEXT NOT NULL,
    size INTEGER DEFAULT 0,
    content_type TEXT DEFAULT '',
    is_image INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    extracted_text TEXT DEFAULT '',
    extraction_version INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_att_item ON attachments(item_id);
"""


def now_cn():
    """当前北京时间（naive，格式 YYYY-MM-DDTHH:MM:SS）"""
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0).isoformat(sep="T", timespec="seconds")


def today_cn():
    """今天（北京时间）YYYY-MM-DD"""
    return datetime.now(TZ).strftime("%Y-%m-%d")


def valid_date(s):
    return bool(s) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))


# ---------------------------------------------------------------- 连接

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # 迁移：旧表补充 extracted_text 列
        cols = {r[1] for r in conn.execute("PRAGMA table_info(attachments)")}
        if cols and "extracted_text" not in cols:
            conn.execute("ALTER TABLE attachments ADD COLUMN extracted_text TEXT DEFAULT ''")
        if cols and "extraction_version" not in cols:
            conn.execute("ALTER TABLE attachments ADD COLUMN extraction_version INTEGER DEFAULT 0")
        # 迁移：旧表补充 is_todo 列；存量事项保持原来的待办行为，新事项默认为笔记
        cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        if cols and "is_todo" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN is_todo INTEGER DEFAULT 0")
            conn.execute("UPDATE items SET is_todo = 1")
        conn.commit()
    finally:
        conn.close()


def checkpoint():
    """合并 WAL 日志，保证数据库单文件可完整备份"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- Twos 核心逻辑

def carry_over_unfinished(today=None):
    """未完成的待办（is_todo）自动顺延到今天（Twos 的核心逻辑），并记录原日期。
    普通笔记（非待办）留在原日期不动。"""
    today = today or today_cn()
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE items SET carried_from = COALESCE(carried_from, date),
                      date = ?, updated_at = ?
               WHERE date IS NOT NULL AND date < ? AND done = 0 AND is_todo = 1""",
            (today, now_cn(), today),
        )
        return cur.rowcount


# ---------------------------------------------------------------- HTML 清洗

ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "del", "ul", "ol", "li",
    "h2", "h3", "h4", "a", "img", "div", "span", "blockquote", "pre", "code", "hr",
}
ALLOWED_ATTRS = {
    "a": {"href", "title", "class", "target", "data-att-id"},
    "img": {"src", "alt", "title", "class", "data-att-id"},
    "div": {"class"},
    "span": {"class"},
    "p": {"class"},
}
VOID_TAGS = {"br", "img", "hr"}


def _safe_url(url):
    if not url:
        return None
    u = url.strip()
    if u.lower().startswith(("javascript:", "data:", "vbscript:")):
        return None
    return u


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            return
        clean = []
        for k, v in attrs:
            k = k.lower()
            if k not in ALLOWED_ATTRS.get(tag, set()):
                continue
            v = v or ""
            if tag == "a" and k == "href":
                v = _safe_url(v)
                if v is None:
                    continue
            if tag == "img" and k == "src":
                v = _safe_url(v)
                if v is None:
                    continue
            clean.append((k, v))
        attr_str = "".join(f' {k}="{v}"' for k, v in clean)
        self.out.append(f"<{tag}{attr_str}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_data(self, data):
        import html as _html
        if data:
            self.out.append(_html.escape(data, quote=False))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self.stack:
            while self.stack and self.stack[-1] != tag:
                self.out.append(f"</{self.stack.pop()}>")
            if self.stack:
                self.stack.pop()
            self.out.append(f"</{tag}>")

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(html):
    if not html:
        return ""
    p = _Sanitizer()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return ""
    return p.result()


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "li", "h2", "h3", "h4", "blockquote", "pre", "br", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def result(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html):
    if not html:
        return ""
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return ""
    return p.result()
