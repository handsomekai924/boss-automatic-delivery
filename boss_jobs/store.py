"""职位入库（SQLite）。

每拿完一页就调 :meth:`JobStore.save_page`——**清洗和入库是同一步**，
不做「先攒完再批量写」。这样中途断网/被风控，已到手的页也已经落盘。

两张表：

``jobs``
    职位主表，主键 ``encrypt_job_id``（接口的全站唯一 id）。
    重复捞到同一职位时按主键 UPSERT，只更新内容与最近页码，不重复插行。

``fetch_pages``
    每页一行的抓取流水：本页原始几条、洗后几条、入库几条、有没有下一页。
    排查「第 3 页怎么少了几条」时先看这张表，不用翻日志。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config as C
from .models import Job, PageResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SaveOutcome:
    """一页入库的结果。"""

    page: int
    inserted: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    encrypt_job_id     TEXT PRIMARY KEY,
    job_name           TEXT NOT NULL,
    brand_name         TEXT NOT NULL,
    location           TEXT NOT NULL DEFAULT '',
    salary_desc        TEXT NOT NULL DEFAULT '',
    job_experience     TEXT NOT NULL DEFAULT '',
    job_degree         TEXT NOT NULL DEFAULT '',
    brand_industry     TEXT NOT NULL DEFAULT '',
    brand_scale_name   TEXT NOT NULL DEFAULT '',
    city_name          TEXT NOT NULL DEFAULT '',
    area_district      TEXT NOT NULL DEFAULT '',
    business_district  TEXT NOT NULL DEFAULT '',
    brand_stage_name   TEXT NOT NULL DEFAULT '',
    job_labels         TEXT NOT NULL DEFAULT '[]',
    skills             TEXT NOT NULL DEFAULT '[]',
    welfare_list       TEXT NOT NULL DEFAULT '[]',
    boss_name          TEXT NOT NULL DEFAULT '',
    boss_title         TEXT NOT NULL DEFAULT '',
    expect_id          TEXT NOT NULL DEFAULT '',
    job_type           INTEGER NOT NULL DEFAULT 0,
    job_valid_status   INTEGER NOT NULL DEFAULT 1,
    security_id        TEXT NOT NULL DEFAULT '',
    lid                TEXT NOT NULL DEFAULT '',
    page               INTEGER NOT NULL DEFAULT 0,
    raw_json           TEXT NOT NULL DEFAULT '',
    fetched_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_brand  ON jobs(brand_name);
CREATE INDEX IF NOT EXISTS idx_jobs_city   ON jobs(city_name);
CREATE INDEX IF NOT EXISTS idx_jobs_page   ON jobs(page);
CREATE INDEX IF NOT EXISTS idx_jobs_salary ON jobs(salary_desc);

CREATE TABLE IF NOT EXISTS fetch_pages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    page           INTEGER NOT NULL,
    raw_count      INTEGER NOT NULL DEFAULT 0,
    kept_count     INTEGER NOT NULL DEFAULT 0,
    dropped_count  INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count  INTEGER NOT NULL DEFAULT 0,
    has_more       INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    fetched_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fetch_pages_page ON fetch_pages(page);
"""

_INSERT_JOB = """
INSERT INTO jobs (
    encrypt_job_id, job_name, brand_name, location, salary_desc,
    job_experience, job_degree, brand_industry, brand_scale_name,
    city_name, area_district, business_district, brand_stage_name,
    job_labels, skills, welfare_list, boss_name, boss_title,
    expect_id, job_type, job_valid_status, security_id, lid,
    page, raw_json, fetched_at
) VALUES (
    :encrypt_job_id, :job_name, :brand_name, :location, :salary_desc,
    :job_experience, :job_degree, :brand_industry, :brand_scale_name,
    :city_name, :area_district, :business_district, :brand_stage_name,
    :job_labels, :skills, :welfare_list, :boss_name, :boss_title,
    :expect_id, :job_type, :job_valid_status, :security_id, :lid,
    :page, :raw_json, :fetched_at
)
ON CONFLICT(encrypt_job_id) DO UPDATE SET
    job_name          = excluded.job_name,
    brand_name        = excluded.brand_name,
    location          = excluded.location,
    salary_desc       = excluded.salary_desc,
    job_experience    = excluded.job_experience,
    job_degree        = excluded.job_degree,
    brand_industry    = excluded.brand_industry,
    brand_scale_name  = excluded.brand_scale_name,
    city_name         = excluded.city_name,
    area_district     = excluded.area_district,
    business_district = excluded.business_district,
    brand_stage_name  = excluded.brand_stage_name,
    job_labels        = excluded.job_labels,
    skills            = excluded.skills,
    welfare_list      = excluded.welfare_list,
    boss_name         = excluded.boss_name,
    boss_title        = excluded.boss_title,
    expect_id         = excluded.expect_id,
    job_type          = excluded.job_type,
    job_valid_status  = excluded.job_valid_status,
    security_id       = excluded.security_id,
    lid               = excluded.lid,
    page              = excluded.page,
    raw_json          = excluded.raw_json,
    fetched_at        = excluded.fetched_at
"""

_INSERT_PAGE = """
INSERT INTO fetch_pages (
    page, raw_count, kept_count, dropped_count,
    inserted_count, updated_count, has_more, note, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class JobStore:
    """SQLite 职位库。

    :param path: 数据库文件路径；``":memory:"`` 走内存库（测试用）
    """

    def __init__(self, path: Path | str = C.DEFAULT_DB_PATH) -> None:
        if str(path) == ":memory:":
            self.path = Path(":memory:")
        else:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def save_page(self, result: PageResult) -> SaveOutcome:
        """把一页的清洗结果立刻入库，同时记一行 ``fetch_pages`` 流水。"""
        inserted = 0
        updated = 0
        skipped = 0
        now = _now()

        with self._conn:
            for job in result.jobs:
                before = self._conn.execute(
                    "SELECT 1 FROM jobs WHERE encrypt_job_id = ?",
                    (job.encrypt_job_id,),
                ).fetchone()
                self._conn.execute(_INSERT_JOB, _job_params(job, now))
                if before is None:
                    inserted += 1
                else:
                    updated += 1

            skipped = result.dropped_count
            self._conn.execute(
                _INSERT_PAGE,
                (
                    result.page,
                    result.raw_count,
                    len(result.jobs),
                    skipped,
                    inserted,
                    updated,
                    1 if result.has_more else 0,
                    "; ".join(result.dropped)[:500],
                    now,
                ),
            )

        logger.info(
            "第 %s 页入库：原始 %s 条 → 洗后 %s 条（新增 %s / 更新 %s / 丢弃 %s）",
            result.page,
            result.raw_count,
            len(result.jobs),
            inserted,
            updated,
            skipped,
        )
        return SaveOutcome(page=result.page, inserted=inserted, updated=updated, skipped=skipped)

    def save_pages(self, results: Iterable[PageResult]) -> list[SaveOutcome]:
        """连续存多页（离线回放用；线上是抓一页存一页）。"""
        return [self.save_page(result) for result in results]

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def count_jobs(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        return int(row["n"]) if row else 0

    def count_pages(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM fetch_pages").fetchone()
        return int(row["n"]) if row else 0

    def get_job(self, encrypt_job_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE encrypt_job_id = ?", (encrypt_job_id,)
        ).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        city: str | None = None,
        keyword: str | None = None,
    ) -> list[Job]:
        """按最近抓取顺序列职位。``keyword`` 同时匹配岗位名与公司名。"""
        sql = "SELECT * FROM jobs"
        where: list[str] = []
        params: list[Any] = []
        if city:
            where.append("city_name = ?")
            params.append(city)
        if keyword:
            where.append("(job_name LIKE ? OR brand_name LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY fetched_at DESC, page ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_pages(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """抓取流水，最近的在前。"""
        rows = self._conn.execute(
            "SELECT * FROM fetch_pages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def summary(self) -> dict[str, Any]:
        """一页人读摘要（CLI ``stats`` 用）。"""
        total = self.count_jobs()
        by_city = self._conn.execute(
            "SELECT city_name, COUNT(*) AS n FROM jobs "
            "GROUP BY city_name ORDER BY n DESC LIMIT 10"
        ).fetchall()
        pages = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(raw_count),0) AS raw, "
            "COALESCE(SUM(kept_count),0) AS kept, "
            "COALESCE(SUM(dropped_count),0) AS dropped "
            "FROM fetch_pages"
        ).fetchone()
        return {
            "db_path": str(self.path),
            "jobs": total,
            "pages": int(pages["n"]) if pages else 0,
            "raw_seen": int(pages["raw"]) if pages else 0,
            "kept_seen": int(pages["kept"]) if pages else 0,
            "dropped_seen": int(pages["dropped"]) if pages else 0,
            "top_cities": [
                {"city": row["city_name"] or "（空）", "count": int(row["n"])}
                for row in by_city
            ],
        }

    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# 行 ↔ 模型
# --------------------------------------------------------------------------- #


def _job_params(job: Job, fetched_at: str) -> dict[str, Any]:
    params = job.to_dict()
    params["job_labels"] = json.dumps(list(job.job_labels), ensure_ascii=False)
    params["skills"] = json.dumps(list(job.skills), ensure_ascii=False)
    params["welfare_list"] = json.dumps(list(job.welfare_list), ensure_ascii=False)
    params["fetched_at"] = fetched_at
    return params


def _row_to_job(row: sqlite3.Row) -> Job:
    data = dict(row)
    return Job(
        job_name=data["job_name"],
        brand_name=data["brand_name"],
        location=data["location"],
        salary_desc=data["salary_desc"],
        job_experience=data["job_experience"],
        job_degree=data["job_degree"],
        brand_industry=data["brand_industry"],
        brand_scale_name=data["brand_scale_name"],
        encrypt_job_id=data["encrypt_job_id"],
        raw_json=data.get("raw_json") or "",
        city_name=data.get("city_name") or "",
        area_district=data.get("area_district") or "",
        business_district=data.get("business_district") or "",
        brand_stage_name=data.get("brand_stage_name") or "",
        job_labels=_json_tuple(data.get("job_labels")),
        skills=_json_tuple(data.get("skills")),
        welfare_list=_json_tuple(data.get("welfare_list")),
        boss_name=data.get("boss_name") or "",
        boss_title=data.get("boss_title") or "",
        expect_id=data.get("expect_id") or "",
        job_type=int(data.get("job_type") or 0),
        job_valid_status=int(data.get("job_valid_status") or 0),
        security_id=data.get("security_id") or "",
        lid=data.get("lid") or "",
        page=int(data.get("page") or 0),
    )


def _json_tuple(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def open_store(path: Path | str | None = None) -> JobStore:
    """按默认路径（或指定路径）打开职位库。"""
    return JobStore(path or C.DEFAULT_DB_PATH)


def saved_jobs(store: JobStore, results: Sequence[PageResult]) -> int:
    """存一批页，返回新增+更新的总条数（给测试/脚本用的薄封装）。"""
    return sum(store.save_page(result).total for result in results)
