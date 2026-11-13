"""附件归档端到端冒烟测试（仅标准库）。

前置：API(8000) 与样本站(8080) 已启动，且归档目录为空（新卷）。
覆盖：多附件归档、SHA-256 校验、下载地址可用、整组替换、
附件下载失败/类型校验失败时保留原归档、故障恢复后无变化确认。
"""
import hashlib
import json
import urllib.request
import urllib.error

API = "http://127.0.0.1:8000"
SITE = "http://127.0.0.1:8080"
LIST_URL = SITE + "/announcements?page=1"


def post(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def get_bytes(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


def get_json(url):
    return json.loads(get_bytes(url).decode())


def get_status(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def archive():
    return post(API + "/archive", {"announcement_list_url": LIST_URL})


def by_id(entries):
    return {e["announcement_url"].rstrip("/").rsplit("/", 1)[-1]: e for e in entries}


def check_files_downloadable(entry):
    for f in entry["files"]:
        blob = get_bytes(f["download_url"])
        assert blob.startswith(b"%PDF-"), f"{f['name']} 下载内容不是 PDF"
        assert hashlib.sha256(blob).hexdigest() == f["sha256"], f"{f['name']} SHA-256 不符"
        assert len(blob) == f["size"]


# 1. 首轮归档：多附件公告归档成功，故障公告标为未完成
r1 = archive()
print("== 首轮归档 ==")
print("status:", r1["status"], "| fetched:", r1["fetched_announcements"])
for a in r1["archived"]:
    print("  archived :", a["announcement_key"], a["state"], [f["name"] for f in a["files"]])
for i in r1["incomplete"]:
    print("  incomplete:", i["announcement_url"].rsplit("/", 1)[-1],
          "retained:", i["previous_archive_retained"],
          [(f["stage"], f["reason"]) for f in i["failures"]])
for f in r1["failures"]:
    print("  FAIL     :", f["stage"], f["url"], f["reason"])

assert r1["status"] == "partial", "故障公告存在时必须为 partial"
assert r1["fetched_announcements"] == 4
arch = by_id(r1["archived"])
assert set(arch) == {"gg-2026-001", "gg-2026-002", "gg-2026-003"}
assert arch["gg-2026-001"]["state"] == "archived"
assert len(arch["gg-2026-001"]["files"]) == 2, "多附件公告应归档 2 个附件"
assert len(arch["gg-2026-002"]["files"]) == 1
assert len(arch["gg-2026-003"]["files"]) == 1
for a in r1["archived"]:
    check_files_downloadable(a)

inc = by_id(r1["incomplete"])
assert set(inc) == {"gg-2026-004"}, "故障公告应标为未完成"
assert inc["gg-2026-004"]["previous_archive_retained"] is False
fail_by_url = {f["url"].rsplit("/", 1)[-1]: f for f in inc["gg-2026-004"]["failures"]}
assert fail_by_url["g15k200-detour-map.pdf"]["stage"] == "download"
assert "404" in fail_by_url["g15k200-detour-map.pdf"]["reason"]
assert fail_by_url["g15k200-briefing.pdf"]["stage"] == "validate", "假冒 PDF 应被类型校验拦截"
assert any(f["stage"] == "detail" and "deleted" in f["url"] for f in r1["failures"])

# 2. 归档清单接口：3 条完整归档
lst = get_json(API + "/archive/announcements")
assert len(lst["announcements"]) == 3
print("== 归档清单 ==", [e["announcement_key"] for e in lst["announcements"]])

# 3. 第二轮：内容无变化 -> 全部 unchanged
r2 = archive()
arch2 = by_id(r2["archived"])
assert all(a["state"] == "unchanged" for a in arch2.values()), "无变化时不应重复归档"
print("== 第二轮：全部 unchanged ✔ ==")

# 4. 附件变更：升级 gg-2026-002 -> 整组替换，旧文件不再可下载
old_002_files = {f["name"]: f for f in arch["gg-2026-002"]["files"]}
rev = post(SITE + "/admin/revise")
print("== 样本站附件升级 ==", rev)
r3 = archive()
arch3 = by_id(r3["archived"])
assert arch3["gg-2026-002"]["state"] == "updated", "附件集合变化必须整组替换"
new_002 = arch3["gg-2026-002"]["files"]
assert len(new_002) == 2 and all("-v2." in f["name"] for f in new_002)
check_files_downloadable(arch3["gg-2026-002"])
assert arch3["gg-2026-001"]["state"] == "unchanged"
assert arch3["gg-2026-003"]["state"] == "unchanged"
for name, f in old_002_files.items():
    assert get_status(f["download_url"]) == 404, f"旧附件 {name} 应随整组替换被移除"
print("== 整组替换 ✔ 旧附件已移除，新附件可下载 ==")

# 5. 故障注入：gg-2026-003 附件 503 -> 本轮未完成，原有完整归档仍可下载
good_003 = arch["gg-2026-003"]["files"][0]
post(SITE + "/admin/outage")
r4 = archive()
inc4 = by_id(r4["incomplete"])
assert "gg-2026-003" in inc4, "附件 503 时该公告应标为未完成"
assert inc4["gg-2026-003"]["previous_archive_retained"] is True
assert inc4["gg-2026-003"]["failures"][0]["stage"] == "download"
assert "503" in inc4["gg-2026-003"]["failures"][0]["reason"]
blob = get_bytes(good_003["download_url"])
assert hashlib.sha256(blob).hexdigest() == good_003["sha256"], "原有完整归档必须仍可下载"
assert by_id(r4["archived"])["gg-2026-001"]["state"] == "unchanged", "其他公告照常归档"
print("== 故障轮次：003 未完成，原归档仍可下载，其他公告不受影响 ✔ ==")

# 6. 故障恢复：内容未变 -> unchanged，归档保持原样
post(SITE + "/admin/outage")
r5 = archive()
assert by_id(r5["archived"])["gg-2026-003"]["state"] == "unchanged"
print("== 故障恢复：003 unchanged ✔ ==")

print("附件归档全部断言通过 ✔")
