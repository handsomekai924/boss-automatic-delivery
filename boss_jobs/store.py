"""职位入库（SQLite）。

每拿完一页就调 `JobStore.save_page`，清洗和入库是同一步；两张表
（`jobs` 主表 + `fetch_pages` 抓取流水）DDL 在 `boss_db`。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import boss_db

from .models import Job, PageResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SaveOutcome:
    """一页入库的结果。

    Attributes:
        page: 页码。
        inserted: 新增条数。
        updated: 更新条数。
        skipped: 丢弃条数。
    """

    page: int
    inserted: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        """新增 + 更新条数。"""
        return self.inserted + self.updated


_INSERT_JOB = """
INSERT INTO jobs (
    encrypt_job_id, job_name, brand_name, location, salary_desc,
    job_experience, job_degree, brand_industry, brand_scale_name,
    city_name, area_district, business_district, brand_stage_name,
    job_labels, skills, welfare_list, boss_name, boss_title,
    expect_id, job_type, job_valid_status, security_id, lid,
    page, raw_json, fetched_at, job_desc, detail_fetched_at,
    jd_status, jd_error, jd_rule_checked, jd_rule_checked_at,
    is_blacklisted, company_blacklisted, jd_blacklisted, blacklist_types,
    matched_rule_ids, matched_rule_snapshot, blacklist_checked_at, auto_delivery_blocked
    -- last_greeted_at 故意不进 INSERT/UPDATE：
    -- 重抓（UPSERT）不改写发送史，30 天冷却以「最近一次成功发送」为准。
    -- 写入只走 update_last_greeted_at()。
) VALUES (
    :encrypt_job_id, :job_name, :brand_name, :location, :salary_desc,
    :job_experience, :job_degree, :brand_industry, :brand_scale_name,
    :city_name, :area_district, :business_district, :brand_stage_name,
    :job_labels, :skills, :welfare_list, :boss_name, :boss_title,
    :expect_id, :job_type, :job_valid_status, :security_id, :lid,
    :page, :raw_json, :fetched_at, :job_desc, :detail_fetched_at,
    :jd_status, :jd_error, :jd_rule_checked, :jd_rule_checked_at,
    :is_blacklisted, :company_blacklisted, :jd_blacklisted, :blacklist_types,
    :matched_rule_ids, :matched_rule_snapshot, :blacklist_checked_at, :auto_delivery_blocked
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
    inserted_count, updated_count, has_more, note, fetched_at, task_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_TASK_JOB = """
INSERT OR IGNORE INTO crawl_task_jobs (task_id, encrypt_job_id, action)
VALUES (?, ?, ?)
"""

_INSERT_CRAWL_TASK = """
INSERT INTO crawl_tasks (
    task_id, source, status, created_at, started_at, ended_at,
    params_json, stats_json, job_count, inserted_count, updated_count,
    error, stopped_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class JobStore:
    """状态库里的职位表。

    Args:
        path: 库文件路径；省略 = `BOSS_DB` = `data/boss.db`。
            `":memory:"` 走内存库（测试用）。
    """

    def __init__(self, path: Path | str | None = None) -> None:
        """初始化。"""
        self.path = boss_db.resolve_db_path(path)
        if str(self.path) == ":memory:":
            self._conn = boss_db.acquire(self.path)
            self._owns_conn = False
        else:
            self._conn = boss_db.connect(self.path)
            self._owns_conn = True

    def save_page(self, result: PageResult, *, task_id: str = "") -> SaveOutcome:
        """把一页的清洗结果立刻入库，同时记一行 `fetch_pages` 流水。

        Args:
            result: 一页清洗结果。
            task_id: 非空时本页职位还会写进 `crawl_task_jobs`。

        Returns:
            本页入库结果。
        """
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
                    action = "inserted"
                else:
                    updated += 1
                    action = "updated"
                if task_id:
                    self._conn.execute(_INSERT_TASK_JOB, (task_id, job.encrypt_job_id, action))

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
                    task_id or "",
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

    def save_pages(self, results: Iterable[PageResult], *, task_id: str = "") -> list[SaveOutcome]:
        """连续存多页（离线回放用；线上是抓一页存一页）。"""
        return [self.save_page(result, task_id=task_id) for result in results]

    def update_job_desc(
        self, encrypt_job_id: str, job_desc: str, *, fetched_at: str | None = None,
        jd_status: str | None = None, jd_error: str = "", jd_rule_checked: bool | None = None,
        jd_rule_checked_at: str | None = None,
    ) -> bool:
        """把详情接口抓到的 JD 写回一条职位。

        同时盖 `detail_fetched_at`；空 JD 也记一笔。职位不在库里返回 `False`。
        """
        cur = self._conn.execute(
            "UPDATE jobs SET job_desc = ?, detail_fetched_at = ?, "
            "jd_status = COALESCE(?, jd_status), jd_error = ?, "
            "jd_rule_checked = COALESCE(?, jd_rule_checked), "
            "jd_rule_checked_at = COALESCE(?, jd_rule_checked_at) "
            "WHERE encrypt_job_id = ?",
            (
                job_desc or "",
                fetched_at or _now(),
                jd_status,
                jd_error or "",
                None if jd_rule_checked is None else (1 if jd_rule_checked else 0),
                jd_rule_checked_at,
                encrypt_job_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def update_blacklist_state(
        self,
        encrypt_job_id: str,
        *,
        company_blacklisted: bool,
        jd_blacklisted: bool,
        matched_rules: Sequence[dict[str, Any]],
        jd_rule_checked: bool,
        jd_status: str,
        auto_delivery_blocked: bool,
        checked_at: str | None = None,
    ) -> bool:
        """持久化当前规则评估结果；历史审计另存，不在这里改写。"""
        rule_snapshot = list(matched_rules)
        types = sorted({str(rule.get("rule_type")) for rule in rule_snapshot if rule.get("rule_type")})
        conn = self._conn
        with conn:
            cur = conn.execute(
                "UPDATE jobs SET company_blacklisted=?, jd_blacklisted=?, is_blacklisted=?, "
                "blacklist_types=?, matched_rule_ids=?, matched_rule_snapshot=?, "
                "blacklist_checked_at=?, jd_rule_checked=?, jd_rule_checked_at=?, jd_status=?, "
                "auto_delivery_blocked=? WHERE encrypt_job_id=?",
                (
                    int(company_blacklisted), int(jd_blacklisted), int(company_blacklisted or jd_blacklisted),
                    json.dumps(types, ensure_ascii=False),
                    json.dumps([r.get("rule_id") for r in rule_snapshot], ensure_ascii=False),
                    json.dumps(rule_snapshot, ensure_ascii=False), checked_at or _now(),
                    int(jd_rule_checked), checked_at or _now(), jd_status,
                    int(auto_delivery_blocked), encrypt_job_id,
                ),
            )
        return cur.rowcount > 0

    def update_last_greeted_at(self, encrypt_job_id: str, greeted_at: str | None = None) -> bool:
        """记一次「招呼语发送成功」。失败/跳过不写，抓取 UPSERT 也不会碰这一列。

        Args:
            encrypt_job_id: 职位 id。
            greeted_at: `%Y-%m-%d %H:%M:%S`；省略 = 服务器本地当前时间。

        Returns:
            有没有真的写到行。
        """
        cur = self._conn.execute(
            "UPDATE jobs SET last_greeted_at = ? WHERE encrypt_job_id = ?",
            (greeted_at or _now(), encrypt_job_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_last_greeted_at(self, encrypt_job_id: str) -> str:
        """该岗位最近一次发送成功时间；读不到 / 从未发送 → 空串。"""
        row = self._conn.execute(
            "SELECT last_greeted_at FROM jobs WHERE encrypt_job_id = ?",
            (encrypt_job_id,),
        ).fetchone()
        return str(row["last_greeted_at"] or "") if row else ""

    def last_greeted_map(self, encrypt_job_ids: Sequence[str]) -> dict[str, str]:
        """批量读发送史（读接口出门前给 match 条目补冷却用）。"""
        ids = [str(i) for i in encrypt_job_ids if i]
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT encrypt_job_id, last_greeted_at FROM jobs "
            f"WHERE encrypt_job_id IN ({placeholders})",
            ids,
        ).fetchall()
        return {str(r["encrypt_job_id"]): str(r["last_greeted_at"] or "") for r in rows}

    def create_crawl_task(
        self,
        task_id: str,
        *,
        source: str = "search",
        status: str = "running",
        created_at: float | None = None,
        started_at: float | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        """`crawl_tasks.start()` 时插一行（status=running）+ 写入配置快照。"""
        now = time.time() if created_at is None else float(created_at)
        self._conn.execute(
            _INSERT_CRAWL_TASK,
            (
                task_id,
                source or "search",
                status or "running",
                now,
                now if started_at is None else float(started_at),
                0.0,
                json.dumps(dict(params or {}), ensure_ascii=False),
                "{}",
                0,
                0,
                0,
                "",
                "",
            ),
        )
        self._conn.commit()

    def finish_crawl_task(
        self,
        task_id: str,
        *,
        status: str,
        ended_at: float | None = None,
        stats: Mapping[str, Any] | None = None,
        error: str = "",
        stopped_reason: str = "",
        job_count: int | None = None,
        inserted_count: int | None = None,
        updated_count: int | None = None,
    ) -> bool:
        """结束时回写状态与统计。`job_count` 等省略则按关联表现算。"""
        with self._conn:
            if job_count is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM crawl_task_jobs WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                job_count = int(row["n"]) if row else 0
            if inserted_count is None or updated_count is None:
                rows = self._conn.execute(
                    "SELECT action, COUNT(*) AS n FROM crawl_task_jobs "
                    "WHERE task_id = ? GROUP BY action",
                    (task_id,),
                ).fetchall()
                counts = {str(r["action"]): int(r["n"]) for r in rows}
                if inserted_count is None:
                    inserted_count = counts.get("inserted", 0)
                if updated_count is None:
                    updated_count = counts.get("updated", 0)
            cur = self._conn.execute(
                "UPDATE crawl_tasks SET status=?, ended_at=?, stats_json=?, error=?, "
                "stopped_reason=?, job_count=?, inserted_count=?, updated_count=? "
                "WHERE task_id=?",
                (
                    status,
                    time.time() if ended_at is None else float(ended_at),
                    json.dumps(dict(stats or {}), ensure_ascii=False),
                    error or "",
                    stopped_reason or "",
                    int(job_count),
                    int(inserted_count),
                    int(updated_count),
                    task_id,
                ),
            )
        return cur.rowcount > 0

    def touch_crawl_task_stats(self, task_id: str, stats: Mapping[str, Any]) -> None:
        """进行中任务的中间统计落库（落库失败不影响抓取本体）。"""
        self._conn.execute(
            "UPDATE crawl_tasks SET stats_json=? WHERE task_id=?",
            (json.dumps(dict(stats), ensure_ascii=False), task_id),
        )
        self._conn.commit()

    def get_crawl_task(self, task_id: str) -> dict[str, Any] | None:
        """按任务号取一行抓取任务；没有回 `None`。"""
        row = self._conn.execute(
            "SELECT * FROM crawl_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _row_to_crawl_task(row) if row else None

    def list_crawl_tasks(self, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """历史任务，最近的在前。进行中任务的实时进度由调用方用内存快照覆盖。"""
        rows = self._conn.execute(
            "SELECT * FROM crawl_tasks ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_crawl_task(row) for row in rows]

    def count_crawl_tasks(self) -> int:
        """抓取任务总数。"""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM crawl_tasks").fetchone()
        return int(row["n"]) if row else 0

    def count_crawl_task_jobs(self, task_id: str, *, only_existing: bool = False) -> int:
        """本批职位数。`only_existing=True` 只数库里还存在的（「匹配本批」用）。"""
        if only_existing:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM crawl_task_jobs c "
                "JOIN jobs j ON j.encrypt_job_id = c.encrypt_job_id "
                "WHERE c.task_id = ?",
                (task_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM crawl_task_jobs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["n"]) if row else 0

    def crawl_task_job_ids(self, task_id: str, *, only_existing: bool = True) -> list[str]:
        """本批职位 id。默认只回库里仍存在的（交集为空就别开匹配）。"""
        if only_existing:
            sql = (
                "SELECT c.encrypt_job_id AS jid FROM crawl_task_jobs c "
                "JOIN jobs j ON j.encrypt_job_id = c.encrypt_job_id "
                "WHERE c.task_id = ? ORDER BY c.rowid"
            )
        else:
            sql = (
                "SELECT encrypt_job_id AS jid FROM crawl_task_jobs "
                "WHERE task_id = ? ORDER BY rowid"
            )
        rows = self._conn.execute(sql, (task_id,)).fetchall()
        return [str(row["jid"]) for row in rows]

    def list_crawl_task_jobs(
        self, task_id: str, *, limit: int = 20, offset: int = 0, only_existing: bool = False
    ) -> list[dict[str, Any]]:
        """本批职位摘要（含归属动作）。`only_existing=True` 只回库里仍存在的。"""
        if only_existing:
            sql = (
                "SELECT j.*, c.encrypt_job_id AS link_job_id, c.action AS crawl_action "
                "FROM crawl_task_jobs c "
                "JOIN jobs j ON j.encrypt_job_id = c.encrypt_job_id "
                "WHERE c.task_id = ? ORDER BY c.rowid LIMIT ? OFFSET ?"
            )
        else:
            sql = (
                "SELECT j.*, c.encrypt_job_id AS link_job_id, c.action AS crawl_action "
                "FROM crawl_task_jobs c "
                "LEFT JOIN jobs j ON j.encrypt_job_id = c.encrypt_job_id "
                "WHERE c.task_id = ? ORDER BY c.rowid LIMIT ? OFFSET ?"
            )
        rows = self._conn.execute(sql, (task_id, limit, offset)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            action = str(data.get("crawl_action") or "inserted")
            link_id = str(data.get("link_job_id") or "")
            if not data.get("encrypt_job_id"):
                out.append({
                    "encrypt_job_id": link_id,
                    "job_name": "",
                    "brand_name": "",
                    "location": "",
                    "salary_desc": "",
                    "action": action,
                    "missing": True,
                })
                continue
            item = _row_to_job(row).to_dict()
            item["action"] = action
            item["missing"] = False
            out.append(item)
        return out

    def count_jobs(self) -> int:
        """职位总条数。"""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
        return int(row["n"]) if row else 0

    def count_pages(self) -> int:
        """抓取流水总条数。"""
        row = self._conn.execute("SELECT COUNT(*) AS n FROM fetch_pages").fetchone()
        return int(row["n"]) if row else 0

    def get_job(self, encrypt_job_id: str) -> Job | None:
        """按主键取一条职位；没有回 `None`。"""
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
        category: str | None = None,
        jd_status: str | None = None,
        stable: bool = False,
    ) -> list[Job]:
        """按最近抓取顺序列职位。`keyword` 同时匹配岗位名与公司名。

        `stable=True` 时按主键排序，批量扫描（规则重检）要用稳定序。
        """
        sql = "SELECT * FROM jobs"
        where, params = _match_where(city=city, keyword=keyword, category=category, jd_status=jd_status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        if stable:
            sql += " ORDER BY encrypt_job_id ASC LIMIT ? OFFSET ?"
        else:
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
        category: str | None = None,
        jd_status: str | None = None,
    ) -> int:
        """按 `list_jobs` 同款条件数职位，翻页前先看总数。"""
        sql = "SELECT COUNT(*) AS n FROM jobs"
        where, params = _match_where(
            city=city, keyword=keyword, category=category, jd_status=jd_status
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def list_jobs_missing_desc(
        self, *, limit: int = 20, offset: int = 0
    ) -> list[Job]:
        """列出还没抓到描述的职位，手动补抓用。

        两类进名单：一次都没抓过、抓过但失败（可重试）。
        已有 JD、或抓过且回了空 JD 的不再列，避免重复撞墙。
        """
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE job_desc = '' "
            "AND (detail_fetched_at = '' OR jd_status = 'failed') "
            "ORDER BY fetched_at DESC, page ASC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_jobs_missing_desc(self) -> int:
        """还没抓到描述的职位条数。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE job_desc = '' "
            "AND (detail_fetched_at = '' OR jd_status = 'failed')"
        ).fetchone()
        return int(row["n"]) if row else 0

    def fetched_desc_ids(self, encrypt_job_ids: Sequence[str]) -> set[str]:
        """这些职位里哪些已经抓过详情；失败的不算抓过。"""
        ids = [str(i) for i in encrypt_job_ids if i]
        if not ids:
            return set()
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT encrypt_job_id FROM jobs "
            f"WHERE encrypt_job_id IN ({placeholders}) AND detail_fetched_at != '' "
            f"AND COALESCE(jd_status, '') != 'failed'",
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
        """一页人读摘要（CLI `stats` 用）。"""
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
        """关掉自开的连接；内存库共用连接不关。"""
        if self._owns_conn:
            self._conn.close()

    def __enter__(self) -> "JobStore":
        """返回自身。"""
        return self

    def __exit__(self, *exc: object) -> None:
        """退出时关连接。"""
        self.close()


def _match_where(
    *,
    city: str | None = None,
    keyword: str | None = None,
    category: str | None = None,
    jd_status: str | None = None,
) -> tuple[list[str], list[Any]]:
    """`list_jobs` / `delete_*` 共用的 WHERE 片段。"""
    where: list[str] = []
    params: list[Any] = []
    if city:
        where.append("city_name = ?")
        params.append(city)
    if keyword:
        where.append("(job_name LIKE ? OR brand_name LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    if category == "ordinary":
        where.append("is_blacklisted = 0")
    elif category == "company_blacklisted":
        where.append("company_blacklisted = 1 AND jd_blacklisted = 0")
    elif category == "jd_blacklisted":
        where.append("jd_blacklisted = 1 AND company_blacklisted = 0")
    elif category == "both":
        where.append("company_blacklisted = 1 AND jd_blacklisted = 1")
    elif category == "jd_not_collected":
        where.append("jd_status IN ('pending', 'collecting', 'unchecked')")
    elif category == "jd_failed":
        where.append("jd_status = 'failed'")
    if jd_status:
        where.append("jd_status = ?")
        params.append(jd_status)
    return where, params


def _job_params(job: Job, fetched_at: str) -> dict[str, Any]:
    """职位 → UPSERT 参数（列表字段序列化成 JSON）。"""
    params = job.to_dict()
    params["job_labels"] = json.dumps(list(job.job_labels), ensure_ascii=False)
    params["skills"] = json.dumps(list(job.skills), ensure_ascii=False)
    params["welfare_list"] = json.dumps(list(job.welfare_list), ensure_ascii=False)
    params["fetched_at"] = fetched_at
    params["job_desc"] = job.job_desc
    params["detail_fetched_at"] = job.detail_fetched_at
    params["jd_status"] = job.jd_status
    params["jd_error"] = job.jd_error
    params["jd_rule_checked"] = int(job.jd_rule_checked)
    params["jd_rule_checked_at"] = job.jd_rule_checked_at
    params["is_blacklisted"] = int(job.is_blacklisted)
    params["company_blacklisted"] = int(job.company_blacklisted)
    params["jd_blacklisted"] = int(job.jd_blacklisted)
    params["blacklist_types"] = json.dumps(list(job.blacklist_types), ensure_ascii=False)
    params["matched_rule_ids"] = json.dumps(list(job.matched_rule_ids), ensure_ascii=False)
    params["matched_rule_snapshot"] = json.dumps(list(job.matched_rule_snapshot), ensure_ascii=False)
    params["blacklist_checked_at"] = job.blacklist_checked_at
    params["auto_delivery_blocked"] = int(job.auto_delivery_blocked)
    return params


def _row_to_job(row: sqlite3.Row) -> Job:
    """数据行 → `Job`。"""
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
        jd_status=data.get("jd_status") or ("completed" if data.get("detail_fetched_at") and data.get("job_desc") else "pending"),
        jd_error=data.get("jd_error") or "",
        jd_rule_checked=bool(data.get("jd_rule_checked")),
        jd_rule_checked_at=data.get("jd_rule_checked_at") or "",
        is_blacklisted=bool(data.get("is_blacklisted")),
        company_blacklisted=bool(data.get("company_blacklisted")),
        jd_blacklisted=bool(data.get("jd_blacklisted")),
        blacklist_types=_json_tuple(data.get("blacklist_types")),
        matched_rule_ids=_json_tuple(data.get("matched_rule_ids")),
        matched_rule_snapshot=_json_dict_tuple(data.get("matched_rule_snapshot")),
        blacklist_checked_at=data.get("blacklist_checked_at") or "",
        auto_delivery_blocked=bool(data.get("auto_delivery_blocked", 1)),
        last_greeted_at=data.get("last_greeted_at") or "",
    )


def _row_to_crawl_task(row: sqlite3.Row) -> dict[str, Any]:
    """抓取任务行 → API 形状（JSON 列解开，时间保持 REAL）。"""
    data = dict(row)
    for key in ("params_json", "stats_json"):
        raw = data.get(key) or "{}"
        try:
            data[key[:-5]] = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            data[key[:-5]] = {}
        data.pop(key, None)
    data["job_count"] = int(data.get("job_count") or 0)
    data["inserted_count"] = int(data.get("inserted_count") or 0)
    data["updated_count"] = int(data.get("updated_count") or 0)
    return data


def _json_tuple(raw: Any) -> tuple[str, ...]:
    """JSON 列 → 字符串元组；坏数据回空元组。"""
    if not raw:
        return ()
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _json_dict_tuple(raw: Any) -> tuple[dict[str, Any], ...]:
    """JSON 列 → dict 元组；坏数据回空元组。"""
    if not raw:
        return ()
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _now() -> str:
    """服务器本地当前时间（`%Y-%m-%d %H:%M:%S`）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def open_store(path: Path | str | None = None) -> JobStore:
    """按默认库（或指定路径）打开职位表。省略 `path` 就是 `BOSS_DB` / `data/boss.db`。"""
    return JobStore(path)


def saved_jobs(store: JobStore, results: Sequence[PageResult]) -> int:
    """存一批页，返回新增+更新的总条数（给测试/脚本用的薄封装）。"""
    return sum(store.save_page(result).total for result in results)
