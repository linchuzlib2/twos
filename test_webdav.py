# -*- coding: utf-8 -*-
"""模拟本机 Office 的 WebDAV 保存流程，验证附件编辑后能写回存储（OSS/本地）"""
import os
import requests

BASE = "http://127.0.0.1:5000"
os.environ["APP_PASSWORD"] = "test123"

s = requests.Session()

# 1. 登录
r = s.post(f"{BASE}/auth/login", json={"password": "test123"})
assert r.ok, r.text

# 2. 建事项 + 上传附件
r = s.post(f"{BASE}/api/items", json={"date": None, "list_id": None,
                                      "content_html": "<p>附件编辑测试</p>"})
item = r.json()
r = s.post(f"{BASE}/api/items/{item['id']}", json={"content_html": "<p>附件编辑测试 v2</p>"})
item = s.get(f"{BASE}/api/items/{item['id']}").json()
item_id = item["id"]

r = s.post(f"{BASE}/api/upload",
           files={"files": ("测试文档.docx", b"OLD-CONTENT-V1", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
           data={"item_id": item_id})
assert r.ok, r.text
att = r.json()["attachments"][0]
print(f"上传成功: id={att['id']} filename={att['filename']}")

# 3. 取全局令牌
tk = s.get(f"{BASE}/api/bootstrap").json()["att_token"]
print(f"att_token={tk}")

# 4. 模拟 Office 打开/保存流程
attk = f"{BASE}/attk/{tk}/{att['id']}/{att['filename']}"
print("URL:", attk)

# OPTIONS
r = s.options(attk)
print(f"OPTIONS -> {r.status_code} DAV={r.headers.get('DAV')} Allow={r.headers.get('Allow')}")
assert r.status_code == 200

# PROPFIND Depth:0
r = s.request("PROPFIND", attk, headers={"Depth": "0"})
print(f"PROPFIND -> {r.status_code}")
assert r.status_code == 207, r.text

# LOCK
r = s.request("LOCK", attk, headers={"Timeout": "Second-60"})
lock_token = r.headers.get("Lock-Token", "").strip("<>")
print(f"LOCK -> {r.status_code} token={lock_token}")
assert r.status_code == 200

# PUT（带锁写入新内容）
r = s.put(attk, data=b"NEW-CONTENT-V2-EDITED-BY-WORD",
          headers={"If": f"(<{lock_token}>)",
                   "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
print(f"PUT -> {r.status_code}")
assert r.status_code == 204, f"{r.status_code} {r.text[:300]}"

# UNLOCK
r = s.request("UNLOCK", attk, headers={"Lock-Token": f"<{lock_token}>"})
print(f"UNLOCK -> {r.status_code}")
assert r.status_code == 204, f"{r.status_code} {r.text[:300]}"

# 5. 验证内容已更新
r = s.get(attk)
assert b"NEW-CONTENT-V2-EDITED-BY-WORD" == r.content, f"内容未更新! {r.content[:50]}"
print("GET: content updated OK")

# 6. 验证 size 已更新
r = s.get(f"{BASE}/api/items/{item_id}")
att2 = [a for a in r.json()["attachments"] if a["id"] == att["id"]][0]
print(f"DB size: {att['size']} -> {att2['size']}")
assert att2["size"] == len(b"NEW-CONTENT-V2-EDITED-BY-WORD")

print("\n=== WebDAV write-back flow ALL PASS ===")
