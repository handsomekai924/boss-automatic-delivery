"""职位入库（SQLite）。

每拿完一页就调 :meth:`JobStore.save_page`——**清洗和入库是同一步**，
不做「先攒完再批量写」。这样中途断网/被风控，已到手的页也已经落盘。

两张表（DDL 在 :mod:`boss_db`，跟登录态/筛选条件同一个库）：

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

import boss_db

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


_INSERT_JOB = """
INSERT INTO jobs (
    encrypt_job_id, job_name, brand_name, location, salary_desc,
    job_experience, job_degree, brand_industry, brand_scale_name,
    city_name, area_district, business_district, brand_stage_name,
    job_labels, skills, welfare_list, boss_name, boss_title,
    expect_id, job_type, job_valid_status, security_id, lid,
    page, raw_json, fetched_at, job_desc, detail_fetched_at
) VALUES (
    :encrypt_job_id, :job_name, :brand_name, :location, :salary_desc,
    :job_experience, :job_degree, :brand_industry, :brand_scale_name,
    :city_name, :area_district, :business_district, :brand_stage_name,
    :job_labels, :skills, :welfare_list, :boss_name, :boss_title,
    :expect_id, :job_type, :job_valid_status, :security_id, :lid,
    :page, :raw_json, :fetched_at, :job_desc, :detail_fetched_at
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
    -- job_desc / detail_fetched_at 故意不进 UPDATE：
    -- 列表接口不回 JD，重抓一页会拿空串把已抓到的描述抹掉。
    -- 写 JD 只走 update_job_desc()。
"""

_INSERT_PAGE = """
INSERT INTO fetch_pages (
    page, raw_count, kept_count, dropped_count,
    inserted_count, updated_count, has_more, note, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class JobStore:
    """状态库里的职位表。

    :param path: 库文件路径；省略 = ``BOSS_DB`` = ``data/boss.db``（调用时才解析，
        所以测试只改一个环境变量就能全局隔离）。``":memory:"`` 走内存库（测试用）。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = boss_db.resolve_db_path(path)
        if str(self.path) == ":memory:":
            # 内存库必须跟 doc 助手共用一个连接，否则各看各的空库
            self._conn = boss_db.acquire(self.path)
            self._owns_conn = False
        else:
            # 文件库各自开连接：web 线程池会并发建 JobStore，共用一个连接不安全
            self._conn = boss_db.connect(self.path)
            self._owns_conn = True


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

    def update_job_desc(
        self, encrypt_job_id: str, job_desc: str, *, fetched_at: str | None = None
    ) -> bool:
        """把详情接口抓到的 JD 写回一条职位。

        同时盖 ``detail_fetched_at``：空 JD 也记一笔，免得下次补抓又翻它一遍。
        :return: 有没有真的写到行（职位不在库里 → ``False``）。
        """
        cur = self._conn.execute(
            "UPDATE jobs SET job_desc = ?, detail_fetched_at = ? WHERE encrypt_job_id = ?",
            (job_desc or "", fetched_at or _now(), encrypt_job_id),
        )
        self._conn.commit()
        return cur.rowcount > 0


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
        where, params = _match_where(city=city, keyword=keyword)
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

    def count_jobs_matching(
        self,
        *,
        city: str | None = None,
        keyword: str | None = None,
    ) -> int:
        """按 :meth:`list_jobs` 同款条件数职位，翻页前先看总数。"""
        sql = "SELECT COUNT(*) AS n FROM jobs"
        where, params = _match_where(city=city, keyword=keyword)
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def list_jobs_missing_desc(
        self, *, limit: int = 20, offset: int = 0
    ) -> list[Job]:
        """列出**还没抓到描述**的职位，手动补抓用。

        两条都不回，保证「已有描述的不再重复获取」：

        - ``job_desc != ''`` —— 已经有 JD 了；
        - ``detail_fetched_at != ''`` —— 抓过（哪怕接口回了空 JD），别每次去撞同一批。
        """
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE job_desc = '' AND detail_fetched_at = '' "
            "ORDER BY fetched_at DESC, page ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_jobs_missing_desc(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE job_desc = '' AND detail_fetched_at = ''"
        ).fetchone()
        return int(row["n"]) if row else 0

    def fetched_desc_ids(self, encrypt_job_ids: Sequence[str]) -> set[str]:
        """批量问「这些职位里哪些已经抓过详情」，回已抓过的 id 集合。

        抓取流程顺带补 JD 时用：一页里已入库、已有描述的**不再重复拉详情**。
        """
        ids = [str(i) for i in encrypt_job_ids if i]
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT encrypt_job_id FROM jobs "
            f"WHERE encrypt_job_id IN ({placeholders}) AND detail_fetched_at != ''",
            ids,
        ).fetchall()
        return {str(row["encrypt_job_id"]) for row in rows}

    def delete_job(self, encrypt_job_id: str) -> bool:
        """删一条职位；返回是否真的删掉了。"""
        cur = self._conn.execute(
            "DELETE FROM jobs WHERE encrypt_job_id = ?", (encrypt_job_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_jobs(self, encrypt_job_ids: Sequence[str]) -> int:
        """批量删职位，返回删除条数。空列表直接返回 0。"""
        ids = [str(i) for i in encrypt_job_ids if i]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = self._conn.execute(
            f"DELETE FROM jobs WHERE encrypt_job_id IN ({placeholders})", ids
        )
        self._conn.commit()
        return int(cur.rowcount)

    def clear_jobs(self, *, city: str | None = None, keyword: str | None = None) -> int:
        """按条件清空职位；两个条件都不给 = 清整张表。返回删除条数。"""
        where, params = _match_where(city=city, keyword=keyword)
        sql = "DELETE FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return int(cur.rowcount)

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


    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()
        # :memory: 是跟 doc 助手共享的连接，留着给下一个使用者

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()




def _match_where(
    *,
    city: str | None = None,
    keyword: str | None = None,
) -> tuple[list[str], list[Any]]:
    """``list_jobs`` / ``delete_*`` 共用的 WHERE 片段。"""
    where: list[str] = []
    params: list[Any] = []
    if city:
        where.append("city_name = ?")
        params.append(city)
    if keyword:
        where.append("(job_name LIKE ? OR brand_name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    return where, params


def _job_params(job: Job, fetched_at: str) -> dict[str, Any]:
    params = job.to_dict()
    params["job_labels"] = json.dumps(list(job.job_labels), ensure_ascii=False)
    params["skills"] = json.dumps(list(job.skills), ensure_ascii=False)
    params["welfare_list"] = json.dumps(list(job.welfare_list), ensure_ascii=False)
    params["fetched_at"] = fetched_at
    params["job_desc"] = job.job_desc
    params["detail_fetched_at"] = job.detail_fetched_at
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
        job_desc=data.get("job_desc") or "",
        detail_fetched_at=data.get("detail_fetched_at") or "",
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
    """按默认库（或指定路径）打开职位表。省略 ``path`` 就是 ``BOSS_DB`` / ``data/boss.db``。"""
    return JobStore(path)


def saved_jobs(store: JobStore, results: Sequence[PageResult]) -> int:
    """存一批页，返回新增+更新的总条数（给测试/脚本用的薄封装）。"""
    return sum(store.save_page(result).total for result in results)
