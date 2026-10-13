"""Collection configuration persisted in the shared SQLite document store."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import boss_db
from boss_filter import load_search_filter, save_search_filter
from boss_filter.search import JobSearchFilter

logger = logging.getLogger(__name__)


@dataclass
class CollectionConfig:
    """Saved crawl settings; search dimensions remain in ``search_filter``.

    来源现在是二选一：``expect_kind`` 非空 → 期望流，空 → 搜索流。
    ``use_search`` 是兼容字段（旧「推荐页」已移除），读到 ``False`` 会归一成
    ``True`` 并留警告——库里不用迁移。
    """

    max_pages: int = 5
    start_page: int = 1
    page_interval: float = 1.0
    #: 兼容字段，恒为 True。旧 false = 已移除的「推荐页」special/zone。
    use_search: bool = True
    fetch_details: bool = True
    detail_interval: float = 1.0
    store_blacklisted: bool = True
    # 期望流（空 = 不启用，走搜索流）
    expect_kind: str = ""            # "" | "recommend" | "part_time" | "expect" | "temp_expect"
    encrypt_expect_id: str = ""      # 期望流主参数；搜索流时也回填（期望+关键词）
    mix_expect_type: str = ""
    expect_info: str = ""            # 仅 temp_expect：expectInfo JSON 串
    expect_name: str = ""            # 仅展示（jobs 页摘要），不进请求参数
    # 城市/薪资/经验/学历/行业/规模等筛选维度**复用现有 search payload**，不另开字段

    def normalize(self) -> "CollectionConfig":
        try:
            max_pages = int(self.max_pages)
        except (TypeError, ValueError):
            max_pages = 5
        try:
            start_page = int(self.start_page)
        except (TypeError, ValueError):
            start_page = 1
        try:
            page_interval = float(self.page_interval)
        except (TypeError, ValueError):
            page_interval = 1.0
        try:
            detail_interval = float(self.detail_interval)
        except (TypeError, ValueError):
            detail_interval = 1.0
        # 旧「推荐页」配置（use_search=false）已移除：归一成搜索流并留警告
        use_search = bool(self.use_search)
        if not use_search:
            logger.warning("旧「推荐页」配置已移除，改用关键词搜索")
            use_search = True
        return CollectionConfig(
            max_pages=max(1, min(200, max_pages)),
            start_page=max(1, start_page),
            page_interval=max(0.0, min(30.0, page_interval)),
            use_search=use_search,
            fetch_details=bool(self.fetch_details),
            detail_interval=max(0.0, min(30.0, detail_interval)),
            store_blacklisted=bool(self.store_blacklisted),
            expect_kind=str(self.expect_kind or "").strip(),
            encrypt_expect_id=str(self.encrypt_expect_id or "").strip(),
            mix_expect_type=str(self.mix_expect_type or "").strip(),
            expect_info=str(self.expect_info or "").strip(),
            expect_name=str(self.expect_name or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self.normalize())


@dataclass(frozen=True)
class CollectionSnapshot:
    """One immutable task-start view of search, crawl settings, and rules."""

    snapshot_id: str
    created_at: float
    search: dict[str, Any]
    collection: dict[str, Any]
    rules: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "search": self.search,
            "collection": self.collection,
            "rules": list(self.rules),
        }


def load_config(path: Path | str | None = None) -> CollectionConfig:
    try:
        raw = boss_db.doc_get_raw(boss_db.DOC_COLLECTION_CONFIG, path)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("采集配置读失败，按默认处理：%s", exc)
        return CollectionConfig()
    if raw is None:
        return CollectionConfig()
    try:
        data = json.loads(raw)
    except ValueError:
        return CollectionConfig()
    if not isinstance(data, dict):
        return CollectionConfig()
    # Accept both the flat document and the nested form used by newer clients.
    crawl = data.get("crawl") if isinstance(data.get("crawl"), dict) else data
    return CollectionConfig(
        max_pages=crawl.get("max_pages", 5),
        start_page=crawl.get("start_page", 1),
        page_interval=crawl.get("page_interval", crawl.get("interval", 1.0)),
        use_search=crawl.get("use_search", True),
        fetch_details=crawl.get("fetch_details", True),
        detail_interval=crawl.get("detail_interval", 1.0),
        store_blacklisted=crawl.get("store_blacklisted", True),
        # 向后兼容：旧库没这几把 key → 默认空串 → 不启用期望流，行为不变
        expect_kind=crawl.get("expect_kind", ""),
        encrypt_expect_id=crawl.get("encrypt_expect_id", ""),
        mix_expect_type=crawl.get("mix_expect_type", ""),
        expect_info=crawl.get("expect_info", ""),
        expect_name=crawl.get("expect_name", ""),
    ).normalize()


def save_config(cfg: CollectionConfig, path: Path | str | None = None) -> CollectionConfig:
    normalized = cfg.normalize()
    boss_db.doc_set(boss_db.DOC_COLLECTION_CONFIG, normalized.to_dict(), path)
    return normalized


def update_config(payload: dict[str, Any], path: Path | str | None = None) -> CollectionConfig:
    current = load_config(path)
    data = current.to_dict()
    if isinstance(payload.get("crawl"), dict):
        data.update(payload["crawl"])
    data.update({k: v for k, v in payload.items() if k != "crawl"})
    return save_config(CollectionConfig(**data), path)


def reset_config(path: Path | str | None = None) -> CollectionConfig:
    return save_config(CollectionConfig(), path)


def search_payload(f: JobSearchFilter) -> dict[str, Any]:
    """搜索条件的快照形状（与站点查询串对齐，camelCase）。

    ``encryptExpectId`` 一并写出：「期望 + 关键词」组合下前端就是这么发的
    （``onSearch`` 的 ``s.encryptExpectId = lastExpectId``）。期望流走
    ``JobExpectFilter.from_search`` 时也从这份 payload 搬筛选维度——
    **期望 id 在这里，不是单独一份**。
    """
    return {
        "query": f.query,
        "city": f.city,
        "jobType": f.job_type,
        "salary": f.salary,
        "experience": list(f.experience),
        "degree": list(f.degree),
        "industry": list(f.industry),
        "scale": list(f.scale),
        "payType": list(f.pay_type),
        "partTime": list(f.part_time),
        "stage": list(f.stage),
        "encryptExpectId": f.encrypt_expect_id,
        "pageSize": f.page_size,
    }


def load_snapshot(path: Path | str | None = None) -> CollectionSnapshot:
    """Read current collection/search/rule state once for a worker."""
    from .blacklist import list_enabled_rules

    f = load_search_filter(path)
    return CollectionSnapshot(
        snapshot_id="cs_" + uuid.uuid4().hex[:12],
        created_at=__import__("time").time(),
        search=search_payload(f),
        collection=load_config(path).to_dict(),
        rules=tuple(list_enabled_rules(path=path)),
    )
