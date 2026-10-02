# -*- coding: utf-8 -*-
"""阿里云 OSS 存储层：
- 附件/图片上传到 OSS（未配置 OSS 时降级为本地存储，方便本地开发）
- SQLite 数据库文件在每次写操作后自动备份到 OSS（第一时间上传）
- 启动时从 OSS 恢复数据库（应对 Render 等平台临时磁盘）
"""
import logging
import os
import threading
import time
import uuid
from datetime import datetime

import oss2

import db

log = logging.getLogger("storage")

OSS_ACCESS_KEY_ID = os.environ.get("OSS_ACCESS_KEY_ID", "").strip()
OSS_ACCESS_KEY_SECRET = os.environ.get("OSS_ACCESS_KEY_SECRET", "").strip()
OSS_BUCKET = os.environ.get("OSS_BUCKET", "").strip()
OSS_ENDPOINT = os.environ.get("OSS_ENDPOINT", "").strip()  # 例如 oss-cn-beijing.aliyuncs.com
OSS_PREFIX = os.environ.get("OSS_PREFIX", "twos/").strip()
OSS_PUBLIC_BASE_URL = os.environ.get("OSS_PUBLIC_BASE_URL", "").strip().rstrip("/")
# 数据库备份在 OSS 上的对象名
OSS_DB_BACKUP_KEY = os.environ.get("OSS_DB_BACKUP_KEY", "").strip() or (OSS_PREFIX + "backup/twos.sqlite")

_bucket = None
_bucket_lock = threading.Lock()

# 本地降级存储目录
LOCAL_FILES_DIR = os.path.join(db.DATA_DIR, "attachments")


def oss_configured():
    return bool(OSS_ACCESS_KEY_ID and OSS_ACCESS_KEY_SECRET and OSS_BUCKET and OSS_ENDPOINT)


def bucket():
    global _bucket
    if _bucket is None:
        with _bucket_lock:
            if _bucket is None:
                auth = oss2.Auth(OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET)
                _bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)
    return _bucket


def _key_url(key):
    if OSS_PUBLIC_BASE_URL:
        return f"{OSS_PUBLIC_BASE_URL}/{key}"
    return f"https://{OSS_BUCKET}.{OSS_ENDPOINT}/{key}"


# ---------------------------------------------------------------- 上传 / 删除 / 下载

def upload_bytes(key, data, content_type="application/octet-stream"):
    """上传字节流，返回 (key, url)。未配置 OSS 时降级保存到本地 data/attachments。"""
    if oss_configured():
        bucket().put_object(key, data, headers={"Content-Type": content_type})
        return key, _key_url(key)
    os.makedirs(LOCAL_FILES_DIR, exist_ok=True)
    path = os.path.join(LOCAL_FILES_DIR, key.replace("/", os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return key, "/local-files/" + key


def delete_key(key):
    try:
        if oss_configured():
            bucket().delete_object(key)
        else:
            path = os.path.join(LOCAL_FILES_DIR, key.replace("/", os.sep))
            if os.path.isfile(path):
                os.remove(path)
    except Exception:
        log.exception("删除 OSS 对象失败: %s", key)


def object_exists(key):
    if not oss_configured():
        return False
    try:
        return bool(bucket().object_exists(key))
    except Exception:
        log.exception("检查 OSS 对象失败: %s", key)
        return False


def object_last_modified(key):
    if not oss_configured():
        return None
    try:
        meta = bucket().head_object(key)
        return meta.last_modified  # datetime (UTC)
    except oss2.exceptions.NotFound:
        return None
    except Exception:
        log.exception("获取 OSS 对象 meta 失败: %s", key)
        return None


def download_to(key, local_path):
    try:
        bucket().get_object_to_file(key, local_path)
        return True
    except Exception:
        log.exception("从 OSS 下载数据库失败: %s", key)
        return False


def upload_file(key, local_path, content_type="application/octet-stream"):
    bucket().put_object_from_file(key, local_path, headers={"Content-Type": content_type})
    return _key_url(key)


def gen_key(filename):
    """按 年/月 生成对象名，避免重名"""
    now = datetime.now(db.TZ)
    safe = filename.replace("\\", "_").replace("/", "_")
    return f"{OSS_PREFIX}attachments/{now:%Y/%m}/{uuid.uuid4().hex}_{safe}"


# ---------------------------------------------------------------- 数据库自动备份

_dirty = threading.Event()
_backup_lock = threading.Lock()


def mark_dirty():
    """标记数据库已修改，备份线程会尽快上传到 OSS"""
    _dirty.set()


def backup_db_now():
    """立即把 SQLite 数据库文件上传到 OSS"""
    if not oss_configured():
        return False
    with _backup_lock:
        try:
            db.checkpoint()  # 合并 WAL，确保单文件完整
            url = upload_file(OSS_DB_BACKUP_KEY, db.DB_PATH, "application/x-sqlite3")
            log.info("数据库已备份到 OSS: %s", url)
            return True
        except Exception:
            log.exception("数据库备份到 OSS 失败")
            return False


def _backup_loop():
    while True:
        if _dirty.wait(timeout=1):
            time.sleep(2)  # 去抖：连续写操作合并为一次上传
            if _dirty.is_set():
                _dirty.clear()
                backup_db_now()


def start_backup_thread():
    if oss_configured():
        t = threading.Thread(target=_backup_loop, daemon=True, name="oss-db-backup")
        t.start()
        log.info("数据库自动备份线程已启动")


def restore_db_if_needed():
    """启动时从 OSS 恢复数据库（Render 磁盘是临时的，重启后需恢复）"""
    if not oss_configured():
        return
    local_exists = os.path.isfile(db.DB_PATH)
    oss_modified = object_last_modified(OSS_DB_BACKUP_KEY)
    try:
        if oss_modified is None:
            if local_exists:
                backup_db_now()  # OSS 上没有，本地有 -> 上传
            return
        if not local_exists:
            # 本地没有 -> 直接下载
            db.checkpoint()
            if download_to(OSS_DB_BACKUP_KEY, db.DB_PATH):
                log.info("已从 OSS 恢复数据库")
            return
        # 都存在 -> 比较修改时间，取较新的
        local_mtime = datetime.utcfromtimestamp(os.path.getmtime(db.DB_PATH))
        if oss_modified > local_mtime:
            db.checkpoint()
            if download_to(OSS_DB_BACKUP_KEY, db.DB_PATH + ".tmp"):
                os.replace(db.DB_PATH + ".tmp", db.DB_PATH)
                log.info("OSS 版本较新，已用 OSS 版本覆盖本地数据库")
        elif local_mtime > oss_modified:
            backup_db_now()
    except Exception:
        log.exception("恢复数据库时出错")
