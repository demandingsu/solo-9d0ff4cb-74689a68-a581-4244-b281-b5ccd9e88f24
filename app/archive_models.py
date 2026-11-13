"""Pydantic 模型：附件归档接口的请求与响应结构。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 归档轮次整体状态：
# - completed  全部公告均归档成功（含无变化的公告）
# - partial    部分公告归档成功，部分未完成或页面采集失败
# - failed     列表页采集失败等原因导致本轮没有任何公告完成归档
ArchiveStatus = Literal["completed", "partial", "failed"]

# 单条公告本轮的归档动作：
# - archived   首次归档（此前无完整归档）
# - updated    附件集合或内容变化，已整组替换为新归档
# - unchanged  附件集合与内容均无变化，保留原归档
AnnouncementState = Literal["archived", "updated", "unchanged"]


class ArchiveRequest(BaseModel):
    """归档请求：只需公告目录（列表页）URL。"""

    announcement_list_url: str = Field(..., description="公告目录（列表页）URL")


class ArchivedFile(BaseModel):
    """一个已归档附件：来源、校验和与可调用下载地址。"""

    name: str = Field(..., description="归档文件名（组内唯一）")
    source_url: str = Field(..., description="附件原始下载地址")
    sha256: str = Field(..., description="归档内容的 SHA-256（十六进制）")
    size: int = Field(..., description="归档内容字节数")
    download_url: str = Field(..., description="可调用的归档下载地址")


class ArchivedAnnouncement(BaseModel):
    """本轮归档成功的一条公告及其附件组。"""

    announcement_url: str
    announcement_key: str = Field(..., description="归档目录名（由公告 URL 派生）")
    title: str
    state: AnnouncementState
    files: list[ArchivedFile] = Field(default_factory=list)


class AttachmentFailure(BaseModel):
    """单个附件的失败记录：下载、文件类型校验或落盘阶段。"""

    url: str
    stage: Literal["download", "validate", "store"]
    reason: str


class IncompleteAnnouncement(BaseModel):
    """本轮未完成的公告：列出失败地址，原有完整归档仍可下载。"""

    announcement_url: str
    title: str
    failures: list[AttachmentFailure] = Field(default_factory=list)
    previous_archive_retained: bool = Field(
        ..., description="是否存在被保留的原有完整归档（仍可通过下载地址获取）"
    )


class PageFailure(BaseModel):
    """列表页/详情页抓取失败记录。"""

    url: str
    stage: Literal["list", "detail"]
    reason: str


class ArchiveResponse(BaseModel):
    status: ArchiveStatus
    fetched_announcements: int = Field(..., description="成功抓取详情页的公告条数")
    archived: list[ArchivedAnnouncement] = Field(default_factory=list)
    incomplete: list[IncompleteAnnouncement] = Field(default_factory=list)
    failures: list[PageFailure] = Field(
        default_factory=list, description="列表页/详情页抓取失败地址"
    )
    message: Optional[str] = None


class ArchiveListEntry(BaseModel):
    """当前归档清单中的一条公告（供复核查询）。"""

    announcement_url: str
    announcement_key: str
    title: str
    archived_at: str
    files: list[ArchivedFile] = Field(default_factory=list)


class ArchiveListResponse(BaseModel):
    announcements: list[ArchiveListEntry] = Field(default_factory=list)
