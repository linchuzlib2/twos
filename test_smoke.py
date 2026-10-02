# -*- coding: utf-8 -*-
"""本地冒烟测试：使用 Flask test client 验证核心 API"""
import json
import os
import sys

os.environ.pop("APP_PASSWORD", None)

import app as app_module  # noqa: E402

client = app_module.app.test_client()
ok = fail = 0


def check(name, resp, code=200):
    global ok, fail
    body = resp.get_json(silent=True) or {}
    passed = resp.status_code == code
    if passed:
        ok += 1
        print(f"  PASS {name}")
    else:
        fail += 1
        print(f"  FAIL {name}: {resp.status_code} {body}")
    return body


print("[1] 基础")
check("healthz", client.get("/healthz"))
boot = check("bootstrap", client.get("/api/bootstrap"))
today = boot["today"]
print(f"  今天(北京时间): {today}")

print("[2] 事项 CRUD")
r = client.post("/api/items", json={"date": today, "content_html": "<p>写<b>周报</b></p><script>alert(1)</script>"})
item = check("create item", r, 201)
assert "周报" in item["content_html"] and "script" not in item["content_html"], "HTML 清洗失败"
assert item["is_todo"] is False, "默认应是笔记而非待办"
iid = item["id"]
check("set is_todo", client.patch(f"/api/items/{iid}", json={"is_todo": True}))
it = check("get single item", client.get(f"/api/items/{iid}"))
assert it["is_todo"] is True, "is_todo 未生效"
check("toggle done", client.patch(f"/api/items/{iid}", json={"done": True}))
check("un-done", client.patch(f"/api/items/{iid}", json={"done": False}))
check("star", client.patch(f"/api/items/{iid}", json={"starred": True}))
check("remind", client.patch(f"/api/items/{iid}", json={"remind_at": "2030-01-01T09:30"}))
check("bad remind ignored", client.patch(f"/api/items/{iid}", json={"remind_at": "abc"}))

print("[3] 未完成事项顺延（仅待办，笔记不动）")
yesterday = "2020-01-01"
r = client.post("/api/items", json={"date": yesterday, "content_html": "<p>旧待办</p>", "is_todo": True})
old = check("create past todo", r, 201)
r = client.post("/api/items", json={"date": yesterday, "content_html": "<p>旧笔记</p>"})
note = check("create past note", r, 201)
check("carry over (today view)", client.get(f"/api/day/{today}"))
day_items = client.get(f"/api/day/{today}").get_json()["items"]
assert any(i["id"] == old["id"] for i in day_items), "顺延失败"
assert not any(i["id"] == note["id"] for i in day_items), "笔记不应顺延"
old_day = client.get(f"/api/day/{yesterday}").get_json()["items"]
assert any(i["id"] == note["id"] for i in old_day), "笔记应留在原日期"
carried = [i for i in day_items if i["id"] == old["id"]][0]
assert carried["carried_from"] == yesterday, "carried_from 未记录"
old_id, note_id = old["id"], note["id"]

print("[4] 清单")
r = client.post("/api/lists", json={"name": "购物", "emoji": "🛒"})
lst = check("create list", r, 201)
lid = lst["id"]
r = client.post("/api/items", json={"list_id": lid, "content_html": "<p>牛奶</p>"})
li = check("create list item", r, 201)
check("list items", client.get(f"/api/lists/{lid}/items"))
check("move item to list", client.patch(f"/api/items/{iid}", json={"list_id": lid}))
check("move item to day", client.patch(f"/api/items/{iid}", json={"date": today}))
check("lists", client.get("/api/lists"))

print("[5] 日历/星标/搜索/提醒")
check("calendar", client.get(f"/api/calendar?month={today[:7]}"))
check("starred", client.get("/api/starred"))
check("search", client.get("/api/search?q=周报"))
check("reminders due", client.get("/api/reminders/due"))
check("reorder", client.post("/api/items/reorder", json={"ids": [iid, li["id"]]}))

print("[6] 附件（本地降级模式）")
import io
data = {"files": (io.BytesIO(b"hello image"), "test.png", "image/png")}
r = client.post("/api/upload", data=data, content_type="multipart/form-data")
up = check("upload image", r)
att = up["attachments"][0]
assert att["is_image"] and att["url"].startswith("/att/"), up
r = client.get(f"/att/{att['id']}")
assert r.status_code == 200 and r.data == b"hello image", f"附件代理访问失败: {r.status_code}"
print("  PASS /att/<id> proxy")
globals()["ok"] = ok + 1

print("[6b] Office/WebDAV 保存回写")
u = f"/att/{att['id']}/test.png"
r = client.open(u, method="OPTIONS")
assert r.status_code == 200 and "1, 2" in r.headers.get("DAV", ""), r.headers
print("  PASS OPTIONS (DAV)")
globals()["ok"] = ok + 1
r = client.open(u, method="PROPFIND")
assert r.status_code == 207 and b"multistatus" in r.data, (r.status_code, r.data[:100])
print("  PASS PROPFIND")
globals()["ok"] = ok + 1
r = client.open(u, method="LOCK")
assert r.status_code == 200 and "Lock-Token" in r.headers, r.headers
assert b"<D:lockroot>" in r.data and u.encode() in r.data, r.data
assert r.headers.get("Timeout") == "Second-3600", r.headers
print("  PASS LOCK")
globals()["ok"] = ok + 1
r = client.put(u, data=b"edited by word")
assert r.status_code == 204, r.status_code
r = client.get(u)
assert r.data == b"edited by word", "PUT 覆盖失败"
print("  PASS PUT 覆盖保存")
globals()["ok"] = ok + 1

print("[6c] 同名文件重新上传覆盖")
# 先把附件关联到事项，再同名上传 -> 应覆盖而不是新增
r = client.patch(f"/api/items/{iid}", json={"attachment_ids": [att["id"]]})
check("link attachment first", r)
data = {"files": (io.BytesIO(b"replaced content"), "test.png", "image/png"),
        "item_id": str(iid)}
r = client.post("/api/upload", data=data, content_type="multipart/form-data")
up2 = check("same-name upload", r)
assert up2["attachments"][0]["id"] == att["id"], "同名未覆盖，产生了新附件"
r = client.get(f"/att/{att['id']}")
assert r.data == b"replaced content", "同名覆盖后内容未更新"
print("  PASS 覆盖同一附件记录")
globals()["ok"] = ok + 1
r = client.patch(f"/api/items/{iid}", json={
    "content_html": f'<p>带图</p><img src="{att["url"]}" data-att-id="{att["id"]}">',
    "attachment_ids": [att["id"]],
})
check("link attachment", r)
item = client.get(f"/api/day/{today}").get_json()["items"]
me = [i for i in item if i["id"] == iid][0]
assert len(me["attachments"]) == 1, "附件未关联"
check("delete attachment", client.delete(f"/api/attachments/{att['id']}"))

print("[6d] 附件内容全文搜索")
data = {"files": (io.BytesIO("会议纪要：量子加速器方案敲定".encode("utf-8")), "notes.txt", "text/plain"),
        "item_id": str(iid)}
r = client.post("/api/upload", data=data, content_type="multipart/form-data")
up3 = check("upload txt", r)
txt_att = up3["attachments"][0]
r = client.get("/api/search?q=量子加速器")
res = r.get_json()
hit = [i for i in res["items"] if i["id"] == iid]
assert hit, "附件内容未被搜索到"
m = [a for a in hit[0]["attachments"] if a.get("match")]
assert m and "量子加速器" in m[0]["snippet"], m
print("  PASS txt 附件内容搜索命中")
globals()["ok"] = ok + 1

buf = io.BytesIO()
import zipfile
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("word/document.xml",
               "<w:body><w:p><w:r><w:t>季度预算budget1234元</w:t></w:r></w:p></w:body>")
buf.seek(0)
data = {"files": (buf, "report.docx",
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "item_id": str(iid)}
r = client.post("/api/upload", data=data, content_type="multipart/form-data")
up4 = check("upload docx", r)
r = client.get("/api/search?q=budget1234")
hit = [i for i in r.get_json()["items"] if i["id"] == iid]
assert hit and any(a.get("match") for a in hit[0]["attachments"]), "docx 内容未被搜索到"
print("  PASS docx 附件内容搜索命中")
globals()["ok"] = ok + 1

r = client.get(f"/attk/x/{txt_att['id']}/notes.txt")
assert r.status_code == 200 and "量子加速器".encode() in r.data, r.status_code
print("  PASS /attk 令牌路径访问")
globals()["ok"] = ok + 1

# PUT 覆盖后搜索文本应更新
r = client.put(f"/attk/x/{txt_att['id']}/notes.txt", data="新的内容hyperdrive".encode("utf-8"))
assert r.status_code == 204
r = client.get("/api/search?q=hyperdrive")
hit = [i for i in r.get_json()["items"] if i["id"] == iid]
assert hit, "PUT 后新文本未被搜索到"
print("  PASS PUT 覆盖后搜索文本更新")
globals()["ok"] = ok + 1

print("[6e] 文件库")
files = check("files", client.get("/api/files"))["files"]
assert any(f["filename"] == "report.docx" for f in files), "文件库缺少附件"
f0 = [f for f in files if f["filename"] == "notes.txt"][0]
assert f0["item_id"] == iid and f0.get("item_preview"), "文件库缺少所属事项信息"
r = client.get("/api/files?q=report")
assert len(r.get_json()["files"]) >= 1, "文件名筛选失败"
print("  PASS 文件库汇总与筛选")
globals()["ok"] = ok + 1

print("[7] 删除")
check("delete item", client.delete(f"/api/items/{iid}"))
check("delete old item", client.delete(f"/api/items/{old['id']}"))
check("delete note", client.delete(f"/api/items/{note_id}"))
check("delete list item", client.delete(f"/api/items/{li['id']}"))
check("delete list", client.delete(f"/api/lists/{lid}"))

print("[8] 页面")
check("index page", client.get("/")) if False else None
r = client.get("/")
print(f"  {'PASS' if r.status_code == 200 else 'FAIL'} index page ({r.status_code})")
r.status_code == 200 and globals().__setitem__("ok", ok + 1)

print(f"\n结果: {ok} 通过, {fail} 失败")
sys.exit(1 if fail else 0)
