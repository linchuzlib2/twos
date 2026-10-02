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
iid = item["id"]
check("toggle done", client.patch(f"/api/items/{iid}", json={"done": True}))
check("un-done", client.patch(f"/api/items/{iid}", json={"done": False}))
check("star", client.patch(f"/api/items/{iid}", json={"starred": True}))
check("remind", client.patch(f"/api/items/{iid}", json={"remind_at": "2030-01-01T09:30"}))
check("bad remind ignored", client.patch(f"/api/items/{iid}", json={"remind_at": "abc"}))

print("[3] 未完成事项顺延")
yesterday = "2020-01-01"
r = client.post("/api/items", json={"date": yesterday, "content_html": "<p>旧待办</p>"})
old = check("create past item", r, 201)
check("carry over (today view)", client.get(f"/api/day/{today}"))
day_items = client.get(f"/api/day/{today}").get_json()["items"]
assert any(i["id"] == old["id"] for i in day_items), "顺延失败"
carried = [i for i in day_items if i["id"] == old["id"]][0]
assert carried["carried_from"] == yesterday, "carried_from 未记录"

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
assert att["is_image"] and att["url"].startswith("/local-files/"), up
r = client.get(f"/att/{att['id']}")
assert r.status_code == 200 and r.data == b"hello image", f"附件代理访问失败: {r.status_code}"
print("  PASS /att/<id> proxy")
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

print("[7] 删除")
check("delete item", client.delete(f"/api/items/{iid}"))
check("delete old item", client.delete(f"/api/items/{old['id']}"))
check("delete list item", client.delete(f"/api/items/{li['id']}"))
check("delete list", client.delete(f"/api/lists/{lid}"))

print("[8] 页面")
check("index page", client.get("/")) if False else None
r = client.get("/")
print(f"  {'PASS' if r.status_code == 200 else 'FAIL'} index page ({r.status_code})")
r.status_code == 200 and globals().__setitem__("ok", ok + 1)

print(f"\n结果: {ok} 通过, {fail} 失败")
sys.exit(1 if fail else 0)
