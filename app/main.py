"""FastAPI 入口：冷链配送施工封闭核对 API + 公告附件归档 API。"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from .archive_models import (
    ArchiveListEntry,
    ArchiveListResponse,
    ArchiveRequest,
    ArchiveResponse,
    ArchivedAnnouncement,
    ArchivedFile,
    AttachmentFailure,
    IncompleteAnnouncement,
    PageFailure,
)
from .archiver import ArchiveStore, archive_announcements
from .models import CheckRequest, CheckResponse
from .scheduler import build_response
from .scraper import scrape

ARCHIVE_DIR = Path(os.environ.get("ARCHIVE_DIR", "/data/archive"))

store = ArchiveStore(ARCHIVE_DIR)
_archive_lock = asyncio.Lock()  # 串行化归档轮次，避免并发写同一卷


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.initialize()  # 建目录、加载清单、清理上轮崩溃遗留的暂存目录
    yield


app = FastAPI(
    title="冷链配送施工封闭核对与附件归档 API",
    description=(
        "施工封闭核对：传入公告列表页 URL 与计划路段，返回受阻路段及公告链接；"
        "附件归档：传入公告目录 URL，抓取同站分页与详情，把每条公告的全部同站 "
        "PDF 附件整组归档到持久卷，返回公告链接、各附件 SHA-256 与可调用下载地址；"
        "附件集合或内容变化时整组替换，下载/校验/落盘失败时保留原有完整归档。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/check", response_model=CheckResponse)
async def check(request: CheckRequest) -> CheckResponse:
    scrape_result = await scrape(request.announcement_list_url)
    return build_response(request.segments, scrape_result)


# ---- 附件归档 ------------------------------------------------------------


def _download_url(request: Request, key: str, name: str) -> str:
    return str(
        request.url_for("download_archived_file", ann_key=key, filename=name)
    )


def _archived_file(request: Request, key: str, rec) -> ArchivedFile:
    return ArchivedFile(
        name=rec.name,
        source_url=rec.source_url,
        sha256=rec.sha256,
        size=rec.size,
        download_url=_download_url(request, key, rec.name),
    )


@app.post("/archive", response_model=ArchiveResponse)
async def archive(request: ArchiveRequest, http_request: Request) -> ArchiveResponse:
    """归档一轮：抓取目录分页与详情，逐公告整组归档全部同站 PDF 附件。"""
    async with _archive_lock:
        result = await archive_announcements(request.announcement_list_url, store)

    archived = [
        ArchivedAnnouncement(
            announcement_url=o.url,
            announcement_key=o.key,
            title=o.title,
            state=o.state,  # type: ignore[arg-type]
            files=[_archived_file(http_request, o.key, f) for f in o.files],
        )
        for o in result.outcomes
    ]
    incomplete = [
        IncompleteAnnouncement(
            announcement_url=i.url,
            title=i.title,
            failures=[
                AttachmentFailure(url=f.url, stage=f.stage, reason=f.reason)  # type: ignore[arg-type]
                for f in i.failures
            ],
            previous_archive_retained=i.previous_archive_retained,
        )
        for i in result.incomplete
    ]
    failures = [
        PageFailure(url=f.url, stage=f.stage, reason=f.reason)  # type: ignore[arg-type]
        for f in result.page_failures
    ]

    has_problem = bool(incomplete or failures)
    if not archived and not result.fetched:
        status, message = "failed", "公告目录采集失败，本轮没有任何公告完成归档。"
    elif has_problem:
        status = "partial"
        message = (
            f"{len(archived)} 条公告已归档，{len(incomplete)} 条未完成"
            "（原有完整归档仍可下载），其余公告照常归档。"
        )
        if failures:
            message += f" 另有 {len(failures)} 个页面抓取失败。"
    else:
        status = "completed"
        message = f"全部 {len(archived)} 条公告的附件组均已归档或确认无变化。"

    return ArchiveResponse(
        status=status,  # type: ignore[arg-type]
        fetched_announcements=result.fetched,
        archived=archived,
        incomplete=incomplete,
        failures=failures,
        message=message,
    )


@app.get("/archive/announcements", response_model=ArchiveListResponse)
async def list_archived(http_request: Request) -> ArchiveListResponse:
    """列出当前持久卷中的完整归档（供复核查询）。"""
    entries = []
    for key, e in sorted(store.entries().items()):
        entries.append(
            ArchiveListEntry(
                announcement_url=e["url"],
                announcement_key=key,
                title=e.get("title", ""),
                archived_at=e.get("archived_at", ""),
                files=[
                    ArchivedFile(
                        name=f["name"],
                        source_url=f["source_url"],
                        sha256=f["sha256"],
                        size=f["size"],
                        download_url=_download_url(http_request, key, f["name"]),
                    )
                    for f in e.get("files", [])
                ],
            )
        )
    return ArchiveListResponse(announcements=entries)


@app.get("/archive/files/{ann_key}/{filename}", name="download_archived_file")
async def download_archived_file(ann_key: str, filename: str) -> FileResponse:
    """下载已归档附件。仅服务清单中登记的完整归档文件，杜绝路径穿越。"""
    entry = store.get_entry(ann_key)
    if entry is None:
        raise HTTPException(status_code=404, detail="归档不存在或已被整组替换")
    known = {f["name"] for f in entry.get("files", [])}
    if filename not in known:
        raise HTTPException(status_code=404, detail="该文件不在此公告的归档组中")
    path = store.file_path(ann_key, filename)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="归档文件缺失（清单与磁盘不一致）")
    return FileResponse(path, media_type="application/pdf", filename=filename)
