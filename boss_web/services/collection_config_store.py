"""采集配置存取：状态库 `doc('collection_config')`，搜索维度仍在 `search_filter`。

来源二选一：`expect_kind` 非空 → 期望流，空 → 搜索流。整包存 JSON、读时
归一化、坏值不报错只回默认；同时兼容扁平与嵌套 `crawl` 两种旧文档形状。
"""

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
    """一次采集的爬取设置；城市/薪资等筛选维度仍走 `search_filter`。

    Attributes:
        max_pages: 最多翻几页。
        start_page: 起始页码（从 1 起）。
        page_interval: 页间隔（秒）。
        use_search: 兼容字段，恒为 `True`；旧 `False` = 已移除的「推荐页」。
        fetch_details: 是否继续抓职位详情。
        detail_interval: 详情条间隔（秒）。
        store_blacklisted: 是否落库命中黑名单的职位。
        expect_kind: 期望流类型；空 = 走搜索流。
        encrypt_expect_id: 期望流主参数；搜索流时也回填。
        mix_expect_type: 混合期望类型。
        expect_info: 仅 `temp_expect` 用的 `expectInfo` JSON 串。
        expect_name: 仅展示（jobs 页摘要），不进请求参数。
    """

    max_pages: int = 5
    start_page: int = 1
    page_interval: float = 1.0
    use_search: bool = True
    fetch_details: bool = True
    detail_interval: float = 1.0
    store_blacklisted: bool = True
    expect_kind: str = ""
    encrypt_expect_id: str = ""
    mix_expect_type: str = ""
    expect_info: str = ""
    expect_name: str = ""

    def normalize(self) -> "CollectionConfig":
        """夹紧数值区间、收干净字符串，并把旧「推荐页」配置改回搜索流。"""
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
        use_search = bool(self.use_search)
        if not use_search:
            logger.warning("旧「推荐页」配置已移除，改用关键词搜索")
            use_search = True
        return CollectionConfig(
            max_pages=max(1, min(10, max_pages)),
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
        """归一化后转普通字典。"""
        return asdict(self.normalize())


@dataclass(frozen=True)
class CollectionSnapshot:
    """任务启动时对搜索、采集设置与黑名单规则的一次不可变快照。

    Attributes:
        snapshot_id: 快照 id。
        created_at: 生成时间（Unix 秒）。
        search: 搜索条件 payload（camelCase）。
        collection: 采集设置。
        rules: 启用中的黑名单规则。
    """

    snapshot_id: str
    created_at: float
    search: dict[str, Any]
    collection: dict[str, Any]
    rules: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """转普通字典，`rules` 摊成 list。"""
        return {
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
            "search": self.search,
            "collection": self.collection,
            "rules": list(self.rules),
        }


def load_config(path: Path | str | None = None) -> CollectionConfig:
    """读状态库里的采集配置；没有 / 坏了 → 默认配置（不报错）。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
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
    crawl = data.get("crawl") if isinstance(data.get("crawl"), dict) else data
    return CollectionConfig(
        max_pages=crawl.get("max_pages", 5),
        start_page=crawl.get("start_page", 1),
        page_interval=crawl.get("page_interval", crawl.get("interval", 1.0)),
        use_search=crawl.get("use_search", True),
        fetch_details=crawl.get("fetch_details", True),
        detail_interval=crawl.get("detail_interval", 1.0),
        store_blacklisted=crawl.get("store_blacklisted", True),
        expect_kind=crawl.get("expect_kind", ""),
        encrypt_expect_id=crawl.get("encrypt_expect_id", ""),
        mix_expect_type=crawl.get("mix_expect_type", ""),
        expect_info=crawl.get("expect_info", ""),
        expect_name=crawl.get("expect_name", ""),
    ).normalize()


def save_config(cfg: CollectionConfig, path: Path | str | None = None) -> CollectionConfig:
    """归一化后写回状态库。

    Args:
        cfg: 待保存的配置。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。

    Returns:
        归一化后的配置。
    """
    normalized = cfg.normalize()
    boss_db.doc_set(boss_db.DOC_COLLECTION_CONFIG, normalized.to_dict(), path)
    return normalized


def update_config(payload: dict[str, Any], path: Path | str | None = None) -> CollectionConfig:
    """合并更新；`crawl` 子字典与顶层字段都接受。

    Args:
        payload: 要改的字段，可嵌在 `crawl` 下。
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
    current = load_config(path)
    data = current.to_dict()
    if isinstance(payload.get("crawl"), dict):
        data.update(payload["crawl"])
    data.update({k: v for k, v in payload.items() if k != "crawl"})
    return save_config(CollectionConfig(**data), path)


def reset_config(path: Path | str | None = None) -> CollectionConfig:
    """恢复默认采集配置。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
    """
    return save_config(CollectionConfig(), path)


def search_payload(f: JobSearchFilter) -> dict[str, Any]:
    """搜索条件的快照形状（与站点查询串对齐，camelCase）。

    `encryptExpectId` 一并写出：期望 + 关键词组合下前端就是这么发的；
    期望流走 `JobExpectFilter.from_search` 时也从这份 payload 搬筛选维度，
    期望 id 不单独存一份。

    Args:
        f: 当前搜索筛选条件。

    Returns:
        camelCase 的搜索条件字典。
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


def load_snapshot(
    path: Path | str | None = None, *, site: str | None = None
) -> CollectionSnapshot:
    """给 worker 一次性读齐当前搜索、采集设置与启用中的黑名单规则。

    Args:
        path: 状态库路径；省略 = `BOSS_DB` = `data/boss.db`。
        site: 只取这个站点生效的黑名单规则（含全站规则）；`None` = 不切。

    Returns:
        不可变的任务启动快照。
    """
    from .blacklist import list_enabled_rules

    f = load_search_filter(path)
    return CollectionSnapshot(
        snapshot_id="cs_" + uuid.uuid4().hex[:12],
        created_at=__import__("time").time(),
        search=search_payload(f),
        collection=load_config(path).to_dict(),
        rules=tuple(list_enabled_rules(site=site, path=path)),
    )
