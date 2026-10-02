# -*- coding: utf-8 -*-
"""Twos 克隆版 —— Flask 主应用
功能：每日清单（未完成自动顺延）、自定义清单、日历、星标、搜索、提醒、
富文本编辑 + 一次上传多个附件/图片（直传阿里云 OSS）、SQLite 实时备份到 OSS。
时间统一使用中国大陆时间（Asia/Shanghai）。
"""
import logging
import os
import re
import secrets

from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, render_template, request, send_from_directory, session)

import db
import storage

logging.basicConfig(level=logging.INFO)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()  # 可选：访问密码，保护线上数据
MAX_FILE_SIZE = 100 * 1024 * 1024  # 单文件 100MB

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 单次请求最多 500MB
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)


@app.before_request
def auth_guard():
    if not APP_PASSWORD:
        return None
    if session.get("auth"):
        return None
    if request.path.startswith("/static") or request.path in ("/login", "/auth/login", "/healthz"):
        return None
    if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
        return jsonify({"error": "unauthorized"}), 401
    return render_template("login.html")


@app.after_request
def auto_backup(resp):
    """所有写操作成功后，第一时间触发数据库备份到 OSS"""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        if resp.status_code < 400:
            storage.mark_dirty()
    return resp


# ---------------------------------------------------------------- 页面

@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/auth/login", methods=["POST"])
def do_login():
    data = request.get_json(silent=True) or {}
    if data.get("password") == APP_PASSWORD:
        session["auth"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "密码错误"}), 401


@app.route("/auth/logout", methods=["POST"])
def do_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/att/<int:att_id>")
def att_file(att_id):
    """附件/图片代理访问：从 OSS（或本地）读取后返回，私有 Bucket 也能正常打开"""
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if not row:
        abort(404)
    try:
        data = storage.read_bytes(row["oss_key"])
    except Exception:
        abort(404)
    disposition = "inline" if row["is_image"] else "attachment"
    resp = Response(data, content_type=row["content_type"] or "application/octet-stream")
    resp.headers["Content-Disposition"] = (
        f"{disposition}; filename*=UTF-8''{quote(row['filename'])}"
    )
    return resp


@app.route("/local-files/<path:key>")
def local_file(key):
    """未配置 OSS 时的本地附件服务（本地开发用）"""
    return send_from_directory(storage.LOCAL_FILES_DIR, key)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "oss": storage.oss_configured()})


# ---------------------------------------------------------------- 工具

def item_row_to_dict(row, with_attachments=True):
    d = dict(row)
    d["done"] = bool(d["done"])
    d["starred"] = bool(d["starred"])
    if with_attachments:
        with db.get_db() as conn:
            atts = conn.execute(
                "SELECT * FROM attachments WHERE item_id = ? ORDER BY id", (d["id"],)
            ).fetchall()
        d["attachments"] = [dict(a) for a in atts]
    return d


def get_item_or_404(item_id):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        abort(404)
    return row


def normalize_remind(v):
    """提醒时间格式：YYYY-MM-DDTHH:MM（北京时间，naive 存储）"""
    if not v:
        return None
    v = str(v).strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", v):
        return v + ":00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", v):
        return v
    return None


def sync_attachments(item_id, att_ids):
    """把编辑器里现有的附件关联到事项；被删除的附件同时从 OSS 删除"""
    with db.get_db() as conn:
        for aid in att_ids:
            conn.execute(
                "UPDATE attachments SET item_id = ? WHERE id = ? AND (item_id IS NULL OR item_id = ?)",
                (item_id, aid, item_id),
            )
        existing = conn.execute("SELECT id, oss_key FROM attachments WHERE item_id = ?", (item_id,)).fetchall()
    keep = set(att_ids)
    for row in existing:
        if row["id"] not in keep:
            with db.get_db() as conn:
                conn.execute("DELETE FROM attachments WHERE id = ?", (row["id"],))
            storage.delete_key(row["oss_key"])


def next_sort(conn, list_id=None, date=None):
    if date:
        row = conn.execute("SELECT COALESCE(MAX(sort), 0) m FROM items WHERE date = ?", (date,)).fetchone()
    else:
        row = conn.execute("SELECT COALESCE(MAX(sort), 0) m FROM items WHERE list_id = ?", (list_id,)).fetchone()
    return (row["m"] or 0) + 1


# ---------------------------------------------------------------- API：启动信息

@app.route("/api/bootstrap")
def api_bootstrap():
    return jsonify({
        "today": db.today_cn(),
        "now": db.now_cn(),
        "lists": all_lists(),
        "auth": bool(session.get("auth")) or not APP_PASSWORD,
    })


def all_lists():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM lists ORDER BY sort, id").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        with db.get_db() as conn:
            d["count"] = conn.execute(
                "SELECT COUNT(*) c FROM items WHERE list_id = ?", (r["id"],)
            ).fetchone()["c"]
        result.append(d)
    return result


# ---------------------------------------------------------------- API：每日清单

@app.route("/api/day/<date>")
def api_day(date):
    if not db.valid_date(date):
        abort(400)
    if date == db.today_cn():
        db.carry_over_unfinished(date)  # 未完成事项自动顺延到今天
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE date = ? ORDER BY done, sort, id", (date,)
        ).fetchall()
    return jsonify({
        "date": date,
        "today": db.today_cn(),
        "items": [item_row_to_dict(r) for r in rows],
    })


# ---------------------------------------------------------------- API：清单

@app.route("/api/lists", methods=["GET", "POST"])
def api_lists():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "名称不能为空"}), 400
        emoji = (data.get("emoji") or "").strip()
        with db.get_db() as conn:
            sort = (conn.execute("SELECT COALESCE(MAX(sort),0) m FROM lists").fetchone()["m"] or 0) + 1
            cur = conn.execute(
                "INSERT INTO lists (name, emoji, sort, created_at) VALUES (?,?,?,?)",
                (name, emoji, sort, db.now_cn()),
            )
            list_id = cur.lastrowid
            row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        return jsonify(dict(row)), 201
    return jsonify(all_lists())


@app.route("/api/lists/<int:list_id>", methods=["PATCH", "DELETE"])
def api_list(list_id):
    if request.method == "DELETE":
        with db.get_db() as conn:
            atts = conn.execute(
                "SELECT oss_key FROM attachments WHERE item_id IN (SELECT id FROM items WHERE list_id = ?)",
                (list_id,),
            ).fetchall()
            conn.execute("DELETE FROM items WHERE list_id = ?", (list_id,))
            conn.execute("DELETE FROM lists WHERE id = ?", (list_id,))
        for a in atts:
            storage.delete_key(a["oss_key"])
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    with db.get_db() as conn:
        if "name" in data:
            conn.execute("UPDATE lists SET name = ? WHERE id = ?", ((data["name"] or "").strip() or "未命名", list_id))
        if "emoji" in data:
            conn.execute("UPDATE lists SET emoji = ? WHERE id = ?", ((data["emoji"] or "").strip(), list_id))
        if "sort" in data:
            conn.execute("UPDATE lists SET sort = ? WHERE id = ?", (int(data["sort"]), list_id))
        row = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.route("/api/lists/<int:list_id>/items")
def api_list_items(list_id):
    with db.get_db() as conn:
        if not conn.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone():
            abort(404)
        rows = conn.execute(
            "SELECT * FROM items WHERE list_id = ? ORDER BY done, sort, id", (list_id,)
        ).fetchall()
    return jsonify({"items": [item_row_to_dict(r) for r in rows]})


# ---------------------------------------------------------------- API：事项

@app.route("/api/items", methods=["POST"])
def api_create_item():
    data = request.get_json(silent=True) or {}
    date = data.get("date")
    list_id = data.get("list_id")
    if not date and not list_id:
        date = db.today_cn()
    if date and not db.valid_date(date):
        return jsonify({"error": "日期格式错误"}), 400
    html = db.sanitize_html(data.get("content_html") or "")
    text = db.html_to_text(html) or (data.get("content_text") or "").strip()
    remind_at = normalize_remind(data.get("remind_at"))
    now = db.now_cn()
    with db.get_db() as conn:
        sort = next_sort(conn, list_id=list_id, date=date)
        cur = conn.execute(
            """INSERT INTO items (list_id, date, content_html, content_text, starred, remind_at, sort, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (list_id, date, html, text, 1 if data.get("starred") else 0, remind_at, sort, now, now),
        )
        item_id = cur.lastrowid
    sync_attachments(item_id, data.get("attachment_ids") or [])
    return jsonify(item_row_to_dict(get_item_or_404(item_id))), 201


@app.route("/api/items/<int:item_id>", methods=["PATCH", "DELETE"])
def api_item(item_id):
    row = get_item_or_404(item_id)
    if request.method == "DELETE":
        for a in item_row_to_dict(row)["attachments"]:
            storage.delete_key(a["oss_key"])
        with db.get_db() as conn:
            conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.execute("DELETE FROM attachments WHERE item_id = ?", (item_id,))
        return jsonify({"ok": True})
    data = request.get_json(silent=True) or {}
    with db.get_db() as conn:
        if "content_html" in data:
            html = db.sanitize_html(data["content_html"] or "")
            conn.execute(
                "UPDATE items SET content_html = ?, content_text = ?, updated_at = ? WHERE id = ?",
                (html, db.html_to_text(html), db.now_cn(), item_id),
            )
        if "done" in data:
            conn.execute(
                "UPDATE items SET done = ?, updated_at = ? WHERE id = ?",
                (1 if data["done"] else 0, db.now_cn(), item_id),
            )
        if "starred" in data:
            conn.execute(
                "UPDATE items SET starred = ? WHERE id = ?",
                (1 if data["starred"] else 0, item_id),
            )
        if "remind_at" in data:
            conn.execute(
                "UPDATE items SET remind_at = ?, notified_at = NULL WHERE id = ?",
                (normalize_remind(data["remind_at"]), item_id),
            )
        # 移动事项：到某一天 / 某个清单
        if "date" in data and data["date"] is not None:
            if not db.valid_date(data["date"]):
                return jsonify({"error": "日期格式错误"}), 400
            conn.execute(
                "UPDATE items SET date = ?, list_id = NULL, sort = ?, updated_at = ? WHERE id = ?",
                (data["date"], next_sort(conn, date=data["date"]), db.now_cn(), item_id),
            )
        if "list_id" in data and data["list_id"] is not None:
            conn.execute(
                "UPDATE items SET list_id = ?, date = NULL, carried_from = NULL, sort = ?, updated_at = ? WHERE id = ?",
                (data["list_id"], next_sort(conn, list_id=data["list_id"]), db.now_cn(), item_id),
            )
    if "attachment_ids" in data:
        sync_attachments(item_id, data["attachment_ids"] or [])
    return jsonify(item_row_to_dict(get_item_or_404(item_id)))


@app.route("/api/items/reorder", methods=["POST"])
def api_reorder():
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    with db.get_db() as conn:
        for i, item_id in enumerate(ids):
            conn.execute("UPDATE items SET sort = ? WHERE id = ?", (i + 1, item_id))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API：日历 / 星标 / 搜索 / 提醒

@app.route("/api/calendar")
def api_calendar():
    month = request.args.get("month", "")
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        month = db.today_cn()[:7]
    start, end = month + "-01", month + "-31"
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT date, COUNT(*) total, SUM(done) done FROM items
               WHERE date IS NOT NULL AND date BETWEEN ? AND ? GROUP BY date""",
            (start, end),
        ).fetchall()
    return jsonify({"month": month, "days": [
        {"date": r["date"], "total": r["total"], "done": r["done"] or 0} for r in rows
    ]})


@app.route("/api/starred")
def api_starred():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM items WHERE starred = 1 ORDER BY updated_at DESC").fetchall()
    return jsonify({"items": [item_row_to_dict(r) for r in rows]})


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"items": []})
    like = f"%{q}%"
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM items WHERE content_text LIKE ? ORDER BY updated_at DESC LIMIT 100", (like,)
        ).fetchall()
    return jsonify({"items": [item_row_to_dict(r) for r in rows]})


@app.route("/api/reminders/due")
def api_reminders_due():
    now = db.now_cn()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM items WHERE remind_at IS NOT NULL AND remind_at <= ?
               AND notified_at IS NULL AND done = 0""",
            (now,),
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE items SET notified_at = ? WHERE id = ?", (now, r["id"]))
    return jsonify({"items": [item_row_to_dict(r, with_attachments=False) for r in rows]})


# ---------------------------------------------------------------- API：附件上传（支持一次多个）

@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "没有文件"}), 400
    item_id = request.form.get("item_id", type=int)
    results = []
    for f in files:
        if not f.filename:
            continue
        data = f.read()
        if len(data) > MAX_FILE_SIZE:
            return jsonify({"error": f"{f.filename} 超过 100MB 限制"}), 413
        content_type = f.content_type or "application/octet-stream"
        filename = f.filename
        key, url = storage.upload_bytes(storage.gen_key(filename), data, content_type)
        is_image = int(content_type.startswith("image/"))
        with db.get_db() as conn:
            cur = conn.execute(
                """INSERT INTO attachments (item_id, filename, oss_key, url, size, content_type, is_image, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, filename, key, url, len(data), content_type, is_image, db.now_cn()),
            )
            aid = cur.lastrowid
            # 附件统一通过 /att/<id> 由服务器代理读取，Bucket 可保持私有
            if storage.oss_configured():
                conn.execute("UPDATE attachments SET url = ? WHERE id = ?", (f"/att/{aid}", aid))
                url = f"/att/{aid}"
        results.append({
            "id": aid, "filename": filename, "url": url, "size": len(data),
            "content_type": content_type, "is_image": bool(is_image),
        })
    if not results:
        return jsonify({"error": "没有文件"}), 400
    return jsonify({"attachments": results})


@app.route("/api/attachments/<int:att_id>", methods=["DELETE"])
def api_delete_attachment(att_id):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        if not row:
            abort(404)
        conn.execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    storage.delete_key(row["oss_key"])
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 错误处理

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return e


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "文件过大"}), 413


# ---------------------------------------------------------------- 启动

def migrate_attachment_urls():
    """一次性迁移：把历史直链（OSS URL）替换为 /att/<id> 代理地址，私有 Bucket 也能访问"""
    with db.get_db() as conn:
        rows = conn.execute("SELECT id, url FROM attachments WHERE url LIKE 'http%'").fetchall()
        for r in rows:
            new_url = f"/att/{r['id']}"
            conn.execute(
                "UPDATE items SET content_html = REPLACE(content_html, ?, ?) WHERE content_html LIKE ?",
                (r["url"], new_url, f"%{r['url']}%"),
            )
            conn.execute("UPDATE attachments SET url = ? WHERE id = ?", (new_url, r["id"]))


db.init_db()
migrate_attachment_urls()
storage.restore_db_if_needed()
storage.start_backup_thread()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
