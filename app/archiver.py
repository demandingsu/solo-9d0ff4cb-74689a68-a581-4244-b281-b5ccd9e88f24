"""附件归档：抓取公告附件、校验、SHA-256、整组原子替换落盘。

存储布局（ARCHIVE_DIR，默认 /data/archive，由 Docker Compose 持久卷挂载）：

    <root>/
      manifest.json                 # 完整归档清单（原子写：临时文件 + os.replace）
      announcements/<key>/<file>    # 每条公告一个附件组目录
      .tmp/<key>-<uuid>/            # 本轮下载暂存，成功后才换入

整组替换语义：
- 一条公告的全部附件先下载到暂存目录并逐个校验（%PDF- 魔数）与计算 SHA-256；
- 全部成功才与清单比对：集合或内容变化时，把旧组目录整体改名到回收目录、
  新组目录整体改名就位，再原子更新清单，最后删除回收目录——新旧文件绝不混用；
- 任一附件下载/校验/落盘失败：删除暂存目录，该公告本轮标为未完成，
  原有完整归档（如有）原样保留、仍可下载，其余公告照常处理。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx

from .parser import AttachmentLink, normalize_url, parse_detail_attachments
from .scraper import REQUEST_TIMEOUT, FetchFailureRecord, fetch_detail_pages

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 单附件上限 50 MiB，防止异常响应撑爆磁盘
PDF_MAGIC = b"%PDF-"
_CHUNK = 64 * 1024


# ---- 结果结构（由 main.py 转成 API 响应模型） ----------------------------


@dataclass
class AttachmentFailureRecord:
    url: str
    stage: str  # download / validate / store
    reason: str


@dataclass
class ArchivedFileRecord:
    name: str
    source_url: str
    sha256: str
    size: int


@dataclass
class AnnouncementOutcome:
    url: str
    key: str
    title: str
    state: str  # archived / updated / unchanged
    files: list[ArchivedFileRecord] = field(default_factory=list)


@dataclass
class IncompleteOutcome:
    url: str
    title: str
    failures: list[AttachmentFailureRecord] = field(default_factory=list)
    previous_archive_retained: bool = False


@dataclass
class ArchiveRunResult:
    outcomes: list[AnnouncementOutcome] = field(default_factory=list)
    incomplete: list[IncompleteOutcome] = field(default_factory=list)
    page_failures: list[FetchFailureRecord] = field(default_factory=list)
    fetched: int = 0


class AttachmentError(Exception):
    """单附件失败：stage ∈ {download, validate, store}。"""

    def __init__(self, stage: str, reason: str):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason


# ---- 持久化存储 ----------------------------------------------------------


class ArchiveStore:
    """归档目录与清单管理。单进程内使用（API 以单 worker 运行）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.ann_dir = self.root / "announcements"
        self.tmp_dir = self.root / ".tmp"
        self.manifest_path = self.root / "manifest.json"
        self._manifest: dict = {"version": 1, "announcements": {}}

    # -- 初始化与崩溃恢复 --

    def initialize(self) -> None:
        """启动时调用：建目录、加载清单、清理上轮崩溃遗留的暂存/回收目录。"""
        self.ann_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._manifest = self._load_manifest()
        self._cleanup_stale()

    def _load_manifest(self) -> dict:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("announcements"), dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError):
            # 清单损坏不阻断服务：按空清单继续，公告会在下一轮重新归档
            pass
        return {"version": 1, "announcements": {}}

    def _cleanup_stale(self) -> None:
        for path in self.tmp_dir.glob("*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
        for path in self.ann_dir.glob("*.trash-*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def _save_manifest(self) -> None:
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.manifest_path)

    # -- 查询 --

    def get_entry(self, key: str) -> Optional[dict]:
        return self._manifest["announcements"].get(key)

    def entries(self) -> dict:
        return self._manifest["announcements"]

    def file_path(self, key: str, name: str) -> Path:
        return self.ann_dir / key / name

    # -- 写入：整组替换 --

    def commit_group(
        self,
        key: str,
        url: str,
        title: str,
        staging: Path,
        files: list[ArchivedFileRecord],
    ) -> str:
        """把暂存组目录整组换入归档。返回 'archived' 或 'updated'。

        与清单比对 (文件名, SHA-256) 集合：相同则丢弃暂存、返回 'unchanged'。
        换入过程：旧组改名回收 -> 新组改名就位 -> 原子写清单 -> 删回收目录。
        """
        existed = self.get_entry(key)
        new_set = {(f.name, f.sha256) for f in files}
        if existed is not None:
            old_set = {(f["name"], f["sha256"]) for f in existed.get("files", [])}
            if old_set == new_set:
                shutil.rmtree(staging, ignore_errors=True)
                return "unchanged"

        final = self.ann_dir / key
        trash = self.ann_dir / f"{key}.trash-{uuid.uuid4().hex[:8]}"
        old_entry = existed
        try:
            if final.exists():
                os.rename(final, trash)
            os.rename(staging, final)
        except OSError as exc:
            # 换入失败：尽量把旧组改回原位，保证原归档仍可下载
            if trash.exists() and not final.exists():
                try:
                    os.rename(trash, final)
                except OSError:
                    pass
            raise AttachmentError("store", f"归档组换入失败: {exc}") from exc

        self._manifest["announcements"][key] = {
            "url": url,
            "title": title,
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "files": [
                {
                    "name": f.name,
                    "source_url": f.source_url,
                    "sha256": f.sha256,
                    "size": f.size,
                }
                for f in files
            ],
        }
        try:
            self._save_manifest()
        except OSError as exc:
            # 清单写盘失败：回滚目录交换与内存清单，保持磁盘与清单一致
            if old_entry is None:
                self._manifest["announcements"].pop(key, None)
            else:
                self._manifest["announcements"][key] = old_entry
            try:
                os.rename(final, staging)  # 新组退回暂存，由调用方清理
                if trash.exists():
                    os.rename(trash, final)
            except OSError:
                pass
            raise AttachmentError("store", f"清单写入失败: {exc}") from exc
        shutil.rmtree(trash, ignore_errors=True)
        return "updated" if existed is not None else "archived"

    def discard_staging(self, staging: Path) -> None:
        shutil.rmtree(staging, ignore_errors=True)


# ---- 命名 ----------------------------------------------------------------


def announcement_key(url: str) -> str:
    """由公告 URL 派生稳定的归档目录名：可读 slug + 短哈希防碰撞。"""
    seg = normalize_url(url).rstrip("/").rsplit("/", 1)[-1] or "announcement"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", seg)[:40].strip("._") or "announcement"
    digest = hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:8]
    return f"{slug}--{digest}"


def safe_filename(name: str) -> str:
    """清洗附件文件名：去路径、去危险字符，保证可安全落盘。"""
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return name or "attachment.pdf"


def _dedupe_name(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    stem, dot, suffix = name.rpartition(".")
    for i in range(2, 1000):
        candidate = f"{stem}-{i}{dot}{suffix}" if dot else f"{name}-{i}"
        if candidate not in taken:
            return candidate
    raise AttachmentError("store", "附件重名过多，无法分配归档文件名")


# ---- 下载与校验 ----------------------------------------------------------


async def download_attachment(
    client: httpx.AsyncClient, link: AttachmentLink, dest: Path
) -> ArchivedFileRecord:
    """下载单个附件到 dest，边下边算 SHA-256，并校验真实文件类型为 PDF。"""
    hasher = hashlib.sha256()
    size = 0
    first_chunk = True
    try:
        async with client.stream("GET", link.url) as resp:
            resp.raise_for_status()
            try:
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        if first_chunk:
                            if not chunk.startswith(PDF_MAGIC):
                                raise AttachmentError(
                                    "validate",
                                    "实际内容不是 PDF（缺少 %PDF- 文件头）",
                                )
                            first_chunk = False
                        size += len(chunk)
                        if size > MAX_ATTACHMENT_BYTES:
                            raise AttachmentError(
                                "download",
                                f"附件超过大小上限 {MAX_ATTACHMENT_BYTES} 字节",
                            )
                        hasher.update(chunk)
                        fh.write(chunk)
            except OSError as exc:
                raise AttachmentError("store", f"附件落盘失败: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise AttachmentError(
            "download", f"HTTP {exc.response.status_code}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise AttachmentError("download", "请求超时") from exc
    except httpx.ConnectError as exc:
        raise AttachmentError("download", "连接失败") from exc
    except httpx.HTTPError as exc:
        raise AttachmentError("download", f"{type(exc).__name__}: {exc}") from exc

    if first_chunk:  # 空响应体
        raise AttachmentError("validate", "附件响应为空，不是有效 PDF")
    return ArchivedFileRecord(
        name=dest.name,
        source_url=link.url,
        sha256=hasher.hexdigest(),
        size=size,
    )


# ---- 归档主流程 ----------------------------------------------------------


async def archive_announcements(
    list_url: str, store: ArchiveStore
) -> ArchiveRunResult:
    """抓取目录页 -> 逐公告下载全部同站 PDF 附件 -> 整组替换归档。"""
    result = ArchiveRunResult()
    pages, page_failures = await fetch_detail_pages(list_url)
    result.page_failures = page_failures
    result.fetched = len(pages)

    headers = {"User-Agent": "coldchain-attachment-archiver/1.0"}
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        for page in pages:
            outcome, incomplete = await _archive_one(client, store, page.url, page.html)
            if outcome is not None:
                result.outcomes.append(outcome)
            if incomplete is not None:
                result.incomplete.append(incomplete)
    return result


async def _archive_one(
    client: httpx.AsyncClient, store: ArchiveStore, url: str, html: str
) -> tuple[Optional[AnnouncementOutcome], Optional[IncompleteOutcome]]:
    """归档单条公告。任一附件失败则整组放弃，保留原有完整归档。"""
    try:
        title, links = parse_detail_attachments(html, url)
    except Exception as exc:  # 解析异常视为该公告本轮失败
        return None, IncompleteOutcome(
            url=url,
            title="",
            failures=[AttachmentFailureRecord(url, "download", f"详情页解析失败: {exc}")],
            previous_archive_retained=store.get_entry(announcement_key(url)) is not None,
        )

    key = announcement_key(url)
    retained = store.get_entry(key) is not None

    if not links:
        # 公告当前无 PDF 附件：不改动既有归档（保守保留供复核），
        # 按清单返回现存文件，保证下载地址仍然可用
        existing = store.get_entry(key)
        files = [
            ArchivedFileRecord(
                name=f["name"], source_url=f["source_url"],
                sha256=f["sha256"], size=f["size"],
            )
            for f in (existing or {}).get("files", [])
        ]
        return AnnouncementOutcome(url=url, key=key, title=title,
                                   state="unchanged", files=files), None

    staging = store.tmp_dir / f"{key}-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return None, IncompleteOutcome(
            url=url,
            title=title,
            failures=[AttachmentFailureRecord(url, "store", f"暂存目录创建失败: {exc}")],
            previous_archive_retained=retained,
        )

    # 逐个下载附件：全部尝试，汇集所有失败地址
    files: list[ArchivedFileRecord] = []
    failures: list[AttachmentFailureRecord] = []
    taken: set[str] = set()
    for link in links:
        try:
            name = _dedupe_name(safe_filename(link.name), taken)
        except AttachmentError as exc:
            failures.append(AttachmentFailureRecord(link.url, exc.stage, exc.reason))
            continue
        taken.add(name)
        try:
            record = await download_attachment(client, link, staging / name)
            files.append(record)
        except AttachmentError as exc:
            failures.append(AttachmentFailureRecord(link.url, exc.stage, exc.reason))

    if failures:
        # 有附件失败：整组放弃，原有完整归档不动
        store.discard_staging(staging)
        return None, IncompleteOutcome(
            url=url, title=title, failures=failures,
            previous_archive_retained=retained,
        )

    try:
        state = store.commit_group(key, url, title, staging, files)
    except AttachmentError as exc:
        store.discard_staging(staging)
        return None, IncompleteOutcome(
            url=url, title=title,
            failures=[AttachmentFailureRecord(url, exc.stage, exc.reason)],
            previous_archive_retained=store.get_entry(key) is not None,
        )

    return AnnouncementOutcome(url=url, key=key, title=title, state=state,
                               files=files), None
