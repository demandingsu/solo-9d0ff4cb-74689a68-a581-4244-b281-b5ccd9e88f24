"""离线样本站：模拟公路施工封闭公告的同站分页列表、详情页与 PDF 附件。

数据全部内置，无外部依赖，供采集/归档 API 离线联调。
页面结构与真实公告站点同构：
- /announcements?page=N   列表页（含“下一页”同站分页）
- /announcement/<id>      详情页（封闭字段 + 附件区，链接同站 PDF）
- /files/<name>.pdf       附件下载（内置生成最小合法 PDF，确定性内容）
- POST /admin/revise      附件变更示例：升级 gg-2026-002 的附件版本
                          （文件名与内容同时变化，用于验证整组替换）
- POST /admin/outage      故障注入开关：切换 gg-2026-003 附件 503，
                          用于验证“本轮未完成 + 原有完整归档仍可下载”

故意保留的故障示例：
- 列表中一个已撤销公告的失效详情链接（404）；
- gg-2026-004 的一个附件 404（下载失败）、一个附件返回 HTML（类型校验失败）。
"""
from __future__ import annotations

from datetime import date

from flask import Flask, Response, abort, jsonify, render_template_string, request

app = Flask(__name__)

LIST_TEMPLATE = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>施工封闭公告列表</title></head>
<body>
<h1>公路施工封闭公告</h1>
<table class="announcement-table">
  <thead><tr><th>公告标题</th><th>发布日期</th></tr></thead>
  <tbody>
  {% for item in items %}
    <tr>
      <td><a href="{{ item.href }}">{{ item.title }}</a></td>
      <td>{{ item.published }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
<div class="pagination">
  {% if page > 1 %}<a rel="prev" href="/announcements?page={{ page - 1 }}">上一页</a>{% endif %}
  {% if has_next %}<a rel="next" href="/announcements?page={{ page + 1 }}">下一页</a>{% endif %}
</div>
</body></html>
"""

DETAIL_TEMPLATE = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{{ ann.title }}</title></head>
<body>
<article class="content">
  <h1>{{ ann.title }}</h1>
  <p>路段编号：{{ ann.road_id }}</p>
  <p>方向：{{ ann.direction }}</p>
  <p>生效日期：{{ ann.effective_text }}</p>
  <p>封闭日：{{ ann.weekday_text }}</p>
  <p>封闭时间：{{ ann.clock_text }}</p>
  <p>管制措施：施工期间该路段禁止车辆通行，请提前绕行。</p>
</article>
{% if attachments %}
<section class="attachments">
  <h2>附件（交通管制图）</h2>
  <ul>
  {% for att in attachments %}
    <li><a href="{{ att.href }}">{{ att.name }}</a></li>
  {% endfor %}
  </ul>
</section>
{% endif %}
</body></html>
"""

ANNOUNCEMENTS = [
    {
        "id": "gg-2026-001",
        "title": "G15沈海高速K123段下行夜间施工封闭公告",
        "published": "2026-11-01",
        "road_id": "G15-K123",
        "direction": "下行",
        "effective_start": date(2026, 11, 1),
        "effective_end": date(2026, 11, 30),
        "effective_text": "2026年11月1日至2026年11月30日",
        "weekday_text": "每周一、周三",
        "clock_text": "22:00 至 次日 05:00（跨午夜，归属当日封闭）",
    },
    {
        "id": "gg-2026-002",
        "title": "G15沈海高速K123段上行白天施工封闭公告",
        "published": "2026-11-02",
        "road_id": "G15-K123",
        "direction": "上行",
        "effective_start": date(2026, 11, 5),
        "effective_end": date(2026, 11, 25),
        "effective_text": "2026年11月5日至2026年11月25日",
        "weekday_text": "每周二、周四",
        "clock_text": "09:00 至 16:30",
    },
    {
        "id": "gg-2026-003",
        "title": "G25长深高速K88段双向周末施工封闭公告",
        "published": "2026-11-03",
        "road_id": "G25-K88",
        "direction": "双向",
        "effective_start": date(2026, 10, 15),
        "effective_end": None,
        "effective_text": "2026年10月15日起长期有效",
        "weekday_text": "每周六、周日",
        "clock_text": "00:00 至 24:00（全天封闭）",
    },
    {
        "id": "gg-2026-004",
        "title": "G15沈海高速K200段下行桥梁施工封闭公告",
        "published": "2026-11-06",
        "road_id": "G15-K200",
        "direction": "下行",
        "effective_start": date(2026, 11, 10),
        "effective_end": date(2026, 12, 10),
        "effective_text": "2026年11月10日至2026年12月10日",
        "weekday_text": "每周五",
        "clock_text": "20:00 至 23:00",
    },
]

PAGE_SIZE = 2
# 故意放在列表里的失效链接，验证采集失败处理
BROKEN_HREF = "/announcement/gg-2026-deleted"

# ---- 附件数据 ------------------------------------------------------------

# 附件变更示例：gg-2026-002 的附件版本（POST /admin/revise 升级）
REVISION = {"v": 1}
# 故障注入开关：开启后 gg-2026-003 的附件返回 503（POST /admin/outage 切换）
OUTAGE = {"on": False}

# 固定附件：公告 id -> [(附件文件名, 展示名)]
STATIC_ATTACHMENTS = {
    "gg-2026-001": [
        ("g15k123-phase1-map.pdf", "G15K123下行一阶段交通管制图.pdf"),
        ("g15k123-phase2-map.pdf", "G15K123下行二阶段交通管制图.pdf"),
    ],
    "gg-2026-003": [
        ("g25k88-weekend-map.pdf", "G25K88周末封闭交通管制图.pdf"),
    ],
    # 故障示例公告：一个正常附件 + 一个 404 附件 + 一个假冒 PDF
    "gg-2026-004": [
        ("g15k200-main-map.pdf", "G15K200桥梁施工交通管制图.pdf"),
        ("g15k200-detour-map.pdf", "G15K200绕行路线图.pdf"),
        ("g15k200-briefing.pdf", "G15K200施工简报.pdf"),
    ],
}

# 永远 404 的附件（下载失败示例）与伪装成 PDF 的附件（类型校验失败示例）
MISSING_FILES = {"g15k200-detour-map.pdf"}
FAKE_FILES = {"g15k200-briefing.pdf"}
# 可被故障注入开关影响的附件
OUTAGE_FILES = {"g25k88-weekend-map.pdf"}


def _attachments_for(ann_id: str) -> list[dict]:
    """详情页附件区内容；gg-2026-002 随版本号变化（附件变更示例）。"""
    if ann_id == "gg-2026-002":
        v = REVISION["v"]
        atts = [
            (f"g15k123-up-map-v{v}.pdf", f"G15K123上行交通管制图-v{v}.pdf"),
        ]
        if v >= 2:
            atts.append(
                (f"g15k123-up-signal-v{v}.pdf", f"G15K123上行信号配时图-v{v}.pdf")
            )
        return [{"href": f"/files/{name}", "name": label} for name, label in atts]
    return [
        {"href": f"/files/{name}", "name": label}
        for name, label in STATIC_ATTACHMENTS.get(ann_id, [])
    ]


def _pdf_bytes(label: str) -> bytes:
    """生成最小合法 PDF（确定性内容，标签嵌入注释行使不同附件内容不同）。"""
    return (
        "%PDF-1.4\n"
        f"% {label}\n"
        "1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        "2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        "3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>>endobj\n"
        "trailer<</Root 1 0 R>>\n"
        "%%EOF\n"
    ).encode("utf-8")


def _all_items() -> list[dict]:
    items = [
        {"href": f"/announcement/{a['id']}", "title": a["title"], "published": a["published"]}
        for a in ANNOUNCEMENTS
    ]
    items.insert(1, {"href": BROKEN_HREF, "title": "【已撤销】临时管制公告", "published": "2026-11-04"})
    return items


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/announcements")
def listing():
    page = max(1, request.args.get("page", default=1, type=int))
    items = _all_items()
    start = (page - 1) * PAGE_SIZE
    page_items = items[start : start + PAGE_SIZE]
    if not page_items and page != 1:
        abort(404)
    has_next = start + PAGE_SIZE < len(items)
    return render_template_string(
        LIST_TEMPLATE, items=page_items, page=page, has_next=has_next
    )


@app.get("/announcement/<ann_id>")
def detail(ann_id: str):
    ann = next((a for a in ANNOUNCEMENTS if a["id"] == ann_id), None)
    if ann is None:
        abort(404)
    return render_template_string(
        DETAIL_TEMPLATE, ann=ann, attachments=_attachments_for(ann_id)
    )


@app.get("/files/<path:fname>")
def files(fname: str):
    if fname in MISSING_FILES:
        abort(404)  # 下载失败示例
    if fname in OUTAGE_FILES and OUTAGE["on"]:
        abort(503)  # 故障注入：原站附件暂时失效
    if fname in FAKE_FILES:
        # 类型校验失败示例：URL 以 .pdf 结尾，实际返回 HTML
        return Response(
            "<html><body><h1>施工简报</h1><p>本页为 HTML 简报，并非 PDF 文件。</p></body></html>",
            mimetype="text/html",
        )
    known = {
        name
        for atts in STATIC_ATTACHMENTS.values()
        for name, _ in atts
    }
    # gg-2026-002 的版本化附件：文件名含版本号，内容随文件名确定
    if fname not in known and not (
        fname.startswith("g15k123-up-") and fname.endswith(".pdf")
    ):
        abort(404)
    return Response(_pdf_bytes(f"交通管制图 {fname}"), mimetype="application/pdf")


@app.post("/admin/revise")
def revise():
    """附件变更示例：升级 gg-2026-002 附件版本（集合与内容同时变化）。"""
    REVISION["v"] += 1
    return jsonify({"revision": REVISION["v"]})


@app.post("/admin/outage")
def outage():
    """故障注入开关：切换 gg-2026-003 附件 503/正常。"""
    OUTAGE["on"] = not OUTAGE["on"]
    return jsonify({"outage": OUTAGE["on"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
