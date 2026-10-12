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
    """Saved crawl settings; search dimensions remain in ``search_filter``."""

    max_pages: int = 5
    start_page: int = 1
    page_interval: float = 1.0
    use_search: bool = True
    fetch_details: bool = True
    detail_interval: float = 1.0
    store_blacklisted: bool = True

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
        return CollectionConfig(
            max_pages=max(1, min(200, max_pages)),
            start_page=max(1, start_page),
            page_interval=max(0.0, min(30.0, page_interval)),
            use_search=bool(self.use_search),
            fetch_details=bool(self.fetch_details),
            detail_interval=max(0.0, min(30.0, detail_interval)),
            store_blacklisted=bool(self.store_blacklisted),
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
