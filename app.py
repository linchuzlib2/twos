# -*- coding: utf-8 -*-
"""Twos 克隆版 —— Flask 主应用
功能：每日清单（未完成自动顺延）、自定义清单、日历、星标、搜索、提醒、
富文本编辑 + 一次上传多个附件/图片（直传阿里云 OSS）、SQLite 实时备份到 OSS。
时间统一使用中国大陆时间（Asia/Shanghai）。
"""
import hashlib
import logging
import os
import re
import secrets
import threading
import uuid as _uuid

from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, render_template, request, send_from_directory, session)
from werkzeug.http import http_date

import db
import extract
import storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()  # 可选：访问密码，保护线上数据
MAX_FILE_SIZE = 100 * 1024 * 1024  # 单文件 100MB

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 单次请求最多 500MB
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)


# 附件全局令牌：必须跨 Render 多个实例保持一致。
# 令牌从 SECRET_KEY 或 APP_PASSWORD 稳定派生；两者都没设置则站点公开，附件也无需令牌。
if os.environ.get("SECRET_KEY") or APP_PASSWORD:
    ATT_TOKEN = hashlib.sha256(
        (os.environ.get("SECRET_KEY", "") + APP_PASSWORD).encode("utf-8")
    ).hexdigest()[:32]
else:
    ATT_TOKEN = ""


@app.before_request
def auth_guard():
    if not APP_PASSWORD:
        return None
    if session.get("auth"):
        return None
    # 附件访问：支持 ?tk= 令牌（本机 Office/WebDAV 保存时没有浏览器会话）
    if request.path.startswith("/attk/"):
        # 令牌在路径里，由 attk 视图校验（Office 会丢弃查询参数，所以令牌必须放路径）
        return None
    if request.path.startswith("/att/"):
        tk = request.args.get("tk", "")
        if tk and tk == ATT_TOKEN:
            return None
        if request.method not in ("GET", "HEAD"):
            return "", 401
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


ATT_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "PROPFIND", "LOCK", "UNLOCK"]


def _dav_prop_response(href, name, size, ctype, is_collection):
    resourcetype = (
        "<D:resourcetype><D:collection/></D:resourcetype>"
        if is_collection else "<D:resourcetype/>"
    )
    return (
        f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>"
        f"<D:displayname>{quote(name)}</D:displayname>"
        f"<D:getcontentlength>{size}</D:getcontentlength>"
        f"<D:getcontenttype>{ctype or 'application/octet-stream'}</D:getcontenttype>"
        f"<D:getlastmodified>{http_date()}</D:getlastmodified>"
        f"<D:creationdate>{db.now_cn()}</D:creationdate>"
        f"{resourcetype}"
        "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>"
    )


@app.route("/att/<int:att_id>", methods=ATT_METHODS)
@app.route("/att/<int:att_id>/", methods=ATT_METHODS)
@app.route("/att/<int:att_id>/<path:fname>", methods=ATT_METHODS)
def att_file(att_id, fname=None):
    """附件代理访问（私有 Bucket 可用）+ 最小 WebDAV 支持：
    - GET/HEAD：查看/下载（图片、PDF 直接在浏览器打开，Office 文件下载）
    - PUT：本机 Word/Excel 保存时写回 -> 覆盖 OSS 原对象
    - OPTIONS/PROPFIND/LOCK/UNLOCK：Office 打开远端文档所需的最小 WebDAV 协议
    """
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
    if not row:
        abort(404)

    m = request.method

    if m == "OPTIONS":
        resp = Response()
        resp.headers["DAV"] = "1, 2"
        resp.headers["Allow"] = "OPTIONS, GET, HEAD, PUT, PROPFIND, LOCK, UNLOCK"
        resp.headers["MS-Author-Via"] = "DAV"
        return resp

    if m == "PROPFIND":
        is_collection = fname is None
        parts = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<D:multistatus xmlns:D="DAV:">']
        parts.append(_dav_prop_response(
            request.path, row["filename"] if fname else str(att_id),
            row["size"], row["content_type"], is_collection))
        if is_collection and request.headers.get("Depth") == "1":
            # 列出集合里的文件本身
            parts.append(_dav_prop_response(
                request.path + quote(row["filename"]), row["filename"],
                row["size"], row["content_type"], False))
        parts.append("</D:multistatus>")
        return Response("".join(parts), status=207, content_type='text/xml; charset="utf-8"')

    if m == "LOCK":
        token = f"opaquelocktoken:{_uuid.uuid4()}"
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
            '<D:locktype><D:write/></D:locktype>'
            '<D:lockscope><D:exclusive/></D:lockscope>'
            '<D:depth>0</D:depth>'
            '<D:timeout>Second-3600</D:timeout>'
            f'<D:locktoken><D:href>{token}</D:href></D:locktoken>'
            '</D:activelock></D:lockdiscovery></D:prop>'
        )
        resp = Response(xml, content_type='text/xml; charset="utf-8"')
        resp.headers["Lock-Token"] = f"<{token}>"
        return resp

    if m == "UNLOCK":
        return "", 204

    if m == "PUT":
        # 本机 Office 保存：覆盖 OSS 上的原文件，并重新提取搜索文本
        data = request.get_data()
        ctype = (request.content_type or row["content_type"] or "application/octet-stream").split(";")[0]
        storage.upload_bytes(row["oss_key"], data, ctype)
        with db.get_db() as conn:
            conn.execute("UPDATE attachments SET size = ?, content_type = ? WHERE id = ?",
                         (len(data), ctype, att_id))
        extract_and_store(att_id, data, row["filename"])
        storage.mark_dirty()
        return "", 204

    # GET / HEAD：读取文件
    try:
        data = storage.read_bytes(row["oss_key"])
    except Exception:
        abort(404)
    lower = (row["filename"] or "").lower()
    inline = bool(row["is_image"]) or row["content_type"] == "application/pdf" or lower.endswith(".pdf")
    disposition = "inline" if inline else "attachment"
    resp = Response(data, content_type=row["content_type"] or "application/octet-stream")
    resp.headers["Content-Disposition"] = (
        f"{disposition}; filename*=UTF-8''{quote(row['filename'])}"
    )
    resp.headers["Cache-Control"] = "no-store"  # 防止 Office/浏览器用旧缓存
    return resp


@app.route("/attk/<tk>/<int:att_id>", methods=ATT_METHODS)
@app.route("/attk/<tk>/<int:att_id>/", methods=ATT_METHODS)
@app.route("/attk/<tk>/<int:att_id>/<path:fname>", methods=ATT_METHODS)
def attk_file(tk, att_id, fname=None):
    """带路径令牌的附件访问：本机 Office 打开/保存时没有浏览器会话，
    且会丢弃 URL 查询参数，所以令牌必须放在路径里。"""
    if APP_PASSWORD and tk != ATT_TOKEN:
        abort(401)
    return att_file(att_id, fname)


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
    d["is_todo"] = bool(d.get("is_todo"))
    if with_attachments:
        with db.get_db() as conn:
            atts = conn.execute(
                "SELECT * FROM attachments WHERE item_id = ? ORDER BY id", (d["id"],)
            ).fetchall()
        d["attachments"] = []
        for a in atts:
            ad = dict(a)
            ad.pop("extracted_text", None)
            d["attachments"].append(ad)
    return d


def extract_and_store(att_id, data, filename):
    """提取附件文本（供全文搜索），失败静默"""
    try:
        text = extract.extract_text(data, filename)
        with db.get_db() as conn:
            conn.execute("UPDATE attachments SET extracted_text = ? WHERE id = ?", (text, att_id))
    except Exception:
        log.exception("附件文本提取失败 id=%s", att_id)


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
        "att_token": ATT_TOKEN,
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
    is_todo = 1 if data.get("is_todo") else 0  # 默认为笔记，用户可主动设为待办
    now = db.now_cn()
    with db.get_db() as conn:
        sort = next_sort(conn, list_id=list_id, date=date)
        cur = conn.execute(
            """INSERT INTO items (list_id, date, content_html, content_text, starred, is_todo, remind_at, sort, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (list_id, date, html, text, 1 if data.get("starred") else 0, is_todo, remind_at, sort, now, now),
        )
        item_id = cur.lastrowid
    sync_attachments(item_id, data.get("attachment_ids") or [])
    return jsonify(item_row_to_dict(get_item_or_404(item_id))), 201


@app.route("/api/items/<int:item_id>", methods=["GET", "PATCH", "DELETE"])
def api_item(item_id):
    row = get_item_or_404(item_id)
    if request.method == "GET":
        return jsonify(item_row_to_dict(row))
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
        if "is_todo" in data:
            conn.execute(
                "UPDATE items SET is_todo = ?, updated_at = ? WHERE id = ?",
                (1 if data["is_todo"] else 0, db.now_cn(), item_id),
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


def make_snippet(text, q):
    """截取关键词附近的片段用于搜索结果展示"""
    tl, ql = text.lower(), q.lower()
    i = tl.find(ql)
    if i < 0:
        return ""
    start = max(0, i - 30)
    snippet = text[start:start + 100].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + snippet + "…"


@app.route("/api/search")
def api_search():
    """全文搜索：匹配事项内容 + 附件提取出的文本内容"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"items": []})
    like = f"%{q}%"
    ql = q.lower()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM items WHERE content_text LIKE ?
               OR EXISTS (SELECT 1 FROM attachments a
                          WHERE a.item_id = items.id AND a.extracted_text LIKE ?)
               ORDER BY updated_at DESC LIMIT 100""",
            (like, like),
        ).fetchall()
        result = []
        for r in rows:
            it = item_row_to_dict(r, with_attachments=False)
            atts = []
            for a in conn.execute(
                "SELECT * FROM attachments WHERE item_id = ? ORDER BY id", (r["id"],)
            ).fetchall():
                ad = dict(a)
                et = ad.pop("extracted_text", "") or ""
                if ql in et.lower():
                    ad["match"] = True
                    ad["snippet"] = make_snippet(et, q)
                atts.append(ad)
            it["attachments"] = atts
            result.append(it)
    return jsonify({"items": result})


@app.route("/api/files")
def api_files():
    """文件库：所有事项/待办的附件汇总，可按文件名筛选"""
    q = (request.args.get("q") or "").strip()
    sql = """SELECT a.id, a.filename, a.url, a.size, a.content_type, a.is_image, a.created_at,
                    a.item_id, i.content_text AS item_text, i.date AS item_date, i.list_id
             FROM attachments a LEFT JOIN items i ON i.id = a.item_id"""
    args = ()
    if q:
        sql += " WHERE a.filename LIKE ?"
        args = (f"%{q}%",)
    sql += " ORDER BY a.id DESC LIMIT 500"
    with db.get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    files = []
    for r in rows:
        d = dict(r)
        d.pop("item_text", None)
        d["item_preview"] = (r["item_text"] or "").split("\n")[0][:60] if r["item_text"] else ""
        files.append(d)
    return jsonify({"files": files})


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
        is_image = int(content_type.startswith("image/"))
        # 同一事项上传同名文件 -> 覆盖原附件（OSS 原对象被覆盖，链接不变）
        old = None
        if item_id:
            with db.get_db() as conn:
                old = conn.execute(
                    "SELECT * FROM attachments WHERE item_id = ? AND filename = ?",
                    (item_id, filename),
                ).fetchone()
        if old:
            storage.upload_bytes(old["oss_key"], data, content_type)
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE attachments SET size = ?, content_type = ?, is_image = ?, url = ? WHERE id = ?",
                    (len(data), content_type, is_image, f"/att/{old['id']}", old["id"]),
                )
            aid = old["id"]
            url = f"/att/{aid}"
        else:
            key, url = storage.upload_bytes(storage.gen_key(filename), data, content_type)
            with db.get_db() as conn:
                cur = conn.execute(
                    """INSERT INTO attachments (item_id, filename, oss_key, url, size, content_type, is_image, created_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (item_id, filename, key, url, len(data), content_type, is_image, db.now_cn()),
                )
                aid = cur.lastrowid
                # 附件统一通过 /att/<id> 由服务器代理读取，Bucket 可保持私有
                conn.execute("UPDATE attachments SET url = ? WHERE id = ?", (f"/att/{aid}", aid))
                url = f"/att/{aid}"
        extract_and_store(aid, data, filename)  # 提取文本供全文搜索
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


def backfill_extraction():
    """后台线程：为没有提取文本的存量附件补提文本（含从 OSS 恢复的老附件）"""
    def run():
        try:
            with db.get_db() as conn:
                rows = conn.execute(
                    "SELECT id, filename, oss_key FROM attachments "
                    "WHERE extracted_text IS NULL OR extracted_text = ''"
                ).fetchall()
            for r in rows:
                try:
                    data = storage.read_bytes(r["oss_key"])
                    extract_and_store(r["id"], data, r["filename"])
                except Exception:
                    log.exception("存量附件文本提取失败 id=%s", r["id"])
            if rows:
                log.info("存量附件文本提取完成：%d 个", len(rows))
        except Exception:
            log.exception("存量附件文本提取任务失败")

    threading.Thread(target=run, daemon=True, name="extract-backfill").start()


db.init_db()
migrate_attachment_urls()
storage.restore_db_if_needed()
storage.start_backup_thread()
backfill_extraction()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
