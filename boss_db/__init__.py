"""统一状态库（SQLite）。

单例文档进 `doc` 整包 JSON，简历/分析/职位建真表。路径覆盖：显式 `path` →
环境变量 `BOSS_DB` → `data/boss.db`；用户点名的 JSON 文件读写不走这里。

Attributes:
    SCHEMA_DOC: `doc` 表里存迁移闸门的行名。
    _MIGRATED_FLAG: 迁移闸门字段。
    _DEPRECATED_ENV: 已废弃的环境变量，设了只提醒、不认。
    _conns: 路径 → 该路径下所有已开连接（`close_db` / `close_all` 收尾用）。
    _migrate_lock: 迁移串行锁。
    _COLUMN_MIGRATIONS: 补列迁移表：表名 → {列名: 建列 DDL 片段}。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Final, Iterator, Mapping
from contextlib import contextmanager

logger = logging.getLogger(__name__)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

DATA_DIR: Final[Path] = PROJECT_ROOT / "data"

DEFAULT_DB_PATH: Final[Path] = DATA_DIR / "boss.db"

DB_ENV: Final[str] = "BOSS_DB"

SCHEMA_DOC: Final[str] = "schema"

DOC_SESSION: Final[str] = "session"
DOC_SEARCH_FILTER: Final[str] = "search_filter"
DOC_STOKEN: Final[str] = "stoken"
DOC_LLM_CONFIG: Final[str] = "llm_config"
DOC_DELIVER_CONFIG: Final[str] = "deliver_config"
DOC_APP_SETTINGS: Final[str] = "app_settings"
DOC_COLLECTION_CONFIG: Final[str] = "collection_config"

_MIGRATED_FLAG: Final[str] = "legacy_imported"

_DEPRECATED_ENV: Final[tuple[str, ...]] = ("BOSS_SEARCH_FILTER", "BOSS_STOKEN_STORE")
_warned_env: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS doc (
    name       TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS resume (
    resume_id  TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    content_md TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_resume_created ON resume(created_at);

CREATE TABLE IF NOT EXISTS analysis (
    analysis_id  TEXT PRIMARY KEY,
    resume_id    TEXT NOT NULL DEFAULT '',
    resume_title TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'done',
    model        TEXT NOT NULL DEFAULT '',
    base_url     TEXT NOT NULL DEFAULT '',
    job_count    INTEGER NOT NULL DEFAULT 0,
    top_score    REAL NOT NULL DEFAULT 0.0,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analysis_created ON analysis(created_at);

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
    fetched_at         TEXT NOT NULL,
    -- 职位描述（JD 正文）：列表接口不回，靠 /wapi/zpgeek/job/detail.json 逐条补。
    -- 旧库要能升级，见 _migrate_columns()（CREATE TABLE IF NOT EXISTS 不会补列）。
    job_desc           TEXT NOT NULL DEFAULT '',
    detail_fetched_at  TEXT NOT NULL DEFAULT '',
    jd_status          TEXT NOT NULL DEFAULT 'pending',
    jd_error           TEXT NOT NULL DEFAULT '',
    jd_rule_checked    INTEGER NOT NULL DEFAULT 0,
    jd_rule_checked_at TEXT NOT NULL DEFAULT '',
    is_blacklisted     INTEGER NOT NULL DEFAULT 0,
    company_blacklisted INTEGER NOT NULL DEFAULT 0,
    jd_blacklisted     INTEGER NOT NULL DEFAULT 0,
    blacklist_types    TEXT NOT NULL DEFAULT '[]',
    matched_rule_ids   TEXT NOT NULL DEFAULT '[]',
    matched_rule_snapshot TEXT NOT NULL DEFAULT '[]',
    blacklist_checked_at TEXT NOT NULL DEFAULT '',
    auto_delivery_blocked INTEGER NOT NULL DEFAULT 1,
    -- 最近一次「发招呼语」成功时间（%Y-%m-%d %H:%M:%S）；30 天发送冷却的唯一依据。
    -- 抓取 UPSERT 故意不覆盖这一列——重抓不该改写发送史。
    last_greeted_at  TEXT NOT NULL DEFAULT ''
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
    fetched_at     TEXT NOT NULL,
    -- 归属哪一次抓取任务；空串 = 上线前的旧流水（不回填）
    task_id        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_fetch_pages_page ON fetch_pages(page);

CREATE TABLE IF NOT EXISTS blacklist_rules (
    rule_id       TEXT PRIMARY KEY,
    rule_type     TEXT NOT NULL,
    pattern       TEXT NOT NULL,
    match_mode    TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blacklist_rules_type_enabled
    ON blacklist_rules(rule_type, enabled);

CREATE TABLE IF NOT EXISTS blacklist_hits (
    log_id        TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL DEFAULT '',
    job_id        TEXT NOT NULL DEFAULT '',
    job_name      TEXT NOT NULL DEFAULT '',
    brand_name    TEXT NOT NULL DEFAULT '',
    rule_id       TEXT NOT NULL DEFAULT '',
    rule_type     TEXT NOT NULL DEFAULT '',
    pattern       TEXT NOT NULL DEFAULT '',
    match_mode    TEXT NOT NULL DEFAULT '',
    match_scope   TEXT NOT NULL DEFAULT '',
    matched_text  TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL DEFAULT 'stored',
    jd_status     TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blacklist_hits_rule ON blacklist_hits(rule_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_hits_job ON blacklist_hits(job_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_hits_task ON blacklist_hits(task_id);

-- 抓取任务档案：每次 start() 插一行，结束时回写状态与统计。
-- 只读追溯流水，不提供删除/编辑；旧数据不回填（上线前的抓取没有档案）。
CREATE TABLE IF NOT EXISTS crawl_tasks (
    task_id        TEXT PRIMARY KEY,
    source         TEXT NOT NULL DEFAULT 'search',   -- search | expect
    status         TEXT NOT NULL DEFAULT 'running',  -- running | done | error | cancelled
    created_at     REAL NOT NULL,
    started_at     REAL NOT NULL,
    ended_at       REAL NOT NULL DEFAULT 0,
    params_json    TEXT NOT NULL DEFAULT '{}',       -- 启动时配置快照（筛选条件、页数、间隔…）
    stats_json     TEXT NOT NULL DEFAULT '{}',       -- 页数 / 插入 / 更新 / 描述 / 黑名单命中…
    job_count      INTEGER NOT NULL DEFAULT 0,       -- 本批去重后职位数
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count  INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    stopped_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_crawl_tasks_created ON crawl_tasks(created_at);

-- 任务 ↔ 岗位归属：一次抓取「带回」了哪些职位（含更新已有职位）。
-- 用关联表而不是给 jobs 加一列：同一职位会被多次抓到，挂在 jobs 上后一次
-- 会把归属顶掉，历史任务的「本批岗位」就残缺了。
CREATE TABLE IF NOT EXISTS crawl_task_jobs (
    task_id        TEXT NOT NULL,
    encrypt_job_id TEXT NOT NULL,
    action         TEXT NOT NULL DEFAULT 'inserted', -- inserted | updated
    PRIMARY KEY (task_id, encrypt_job_id)
);

CREATE INDEX IF NOT EXISTS idx_crawl_task_jobs_job ON crawl_task_jobs(encrypt_job_id);

-- 简历优化：一次「按目标岗位改一版」的完整生命周期（草稿 / 已保存 / 丢弃 / 出错）。
-- 一行 = 一次修改意图；原简历永不覆盖，确认保存才产生新的 resume 行。
CREATE TABLE IF NOT EXISTS resume_revisions (
    revision_id      TEXT PRIMARY KEY,           -- rv_ + uuid hex[:10]
    resume_id        TEXT NOT NULL,              -- 底稿简历 id
    resume_title     TEXT NOT NULL DEFAULT '',   -- 底稿标题快照
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',  -- running | draft | saved | discarded | error
    target_kind      TEXT NOT NULL DEFAULT 'jobs',     -- jobs | crawl_task
    target_json      TEXT NOT NULL DEFAULT '{}',       -- 目标岗位快照（自包含）
    crawl_task_id    TEXT NOT NULL DEFAULT '',
    job_count        INTEGER NOT NULL DEFAULT 0,
    original_md      TEXT NOT NULL DEFAULT '',    -- 生成时刻的原文快照
    revised_md       TEXT NOT NULL DEFAULT '',    -- 修改稿全文（草稿 / 已保存都有）
    change_json      TEXT NOT NULL DEFAULT '[]',  -- LLM 变更点清单
    model            TEXT NOT NULL DEFAULT '',
    export_format    TEXT NOT NULL DEFAULT 'md',  -- md | txt
    saved_path       TEXT NOT NULL DEFAULT '',    -- 写盘绝对路径
    saved_resume_id  TEXT NOT NULL DEFAULT '',    -- 入库后的新简历 id
    error            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_resume_revisions_created ON resume_revisions(created_at);
CREATE INDEX IF NOT EXISTS idx_resume_revisions_resume ON resume_revisions(resume_id);
"""


def resolve_db_path(path: Path | str | None = None) -> Path:
    """显式 `path` → `BOSS_DB` → `data/boss.db`；`:memory:` 原样透传。

    调用时才解析，不把默认值冻在签名里——测试只改一个环境变量就能全局隔离。
    """
    for env in _DEPRECATED_ENV:
        if os.environ.get(env) and env not in _warned_env:
            _warned_env.add(env)
            logger.warning("%s 已废弃，改成设 %s", env, DB_ENV)
    if path is not None:
        return Path(":memory:") if str(path) == ":memory:" else Path(path)
    env = os.environ.get(DB_ENV)
    if env:
        return Path(":memory:") if env == ":memory:" else Path(env)
    return DEFAULT_DB_PATH


_lock = threading.Lock()
_conns: dict[str, list[sqlite3.Connection]] = {}
_local = threading.local()
_migrate_lock = threading.Lock()


def _thread_conns() -> dict[str, sqlite3.Connection]:
    """当前线程的「路径 → 连接」表。"""
    d = getattr(_local, "conns", None)
    if d is None:
        d = {}
        _local.conns = d
    return d


@contextmanager
def db(path: Path | str | None = None, *, migrate: bool = True) -> Iterator[sqlite3.Connection]:
    """借当前线程的连接，用完不 close（线程复用）。"""
    conn = acquire(path, migrate=migrate)
    try:
        yield conn
    finally:
        pass


def acquire(path: Path | str | None = None, *, migrate: bool = True) -> sqlite3.Connection:
    """拿当前线程的连接（缓存命中就不重开）。调用方不用 close。"""
    resolved = resolve_db_path(path)
    key = str(resolved)

    tc = _thread_conns()
    conn = tc.get(key)
    if conn is not None:
        return conn

    with _lock:
        conn = _open(resolved, migrate=migrate)
        _conns.setdefault(key, []).append(conn)

    tc[key] = conn
    return conn


def connect(path: Path | str | None = None, *, migrate: bool = True) -> sqlite3.Connection:
    """新开一个连接（调用方负责 close）。给想独占事务的场合用。"""
    return _open(resolve_db_path(path), migrate=migrate)


def close_db(path: Path | str | None = None) -> None:
    """关掉该路径下所有线程的连接（测试里让 `tmp_path` 能被清理掉）。"""
    key = str(resolve_db_path(path))
    with _lock:
        conns = _conns.pop(key, [])
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    tc = _thread_conns()
    tc.pop(key, None)


def close_all() -> None:
    """把所有线程开过的连接都关掉（测试收尾用）。"""
    with _lock:
        all_conns = [c for conns in _conns.values() for c in conns]
        _conns.clear()
    for conn in all_conns:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conns = {}


_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "jobs": {
        "job_desc": "TEXT NOT NULL DEFAULT ''",
        "detail_fetched_at": "TEXT NOT NULL DEFAULT ''",
        "jd_status": "TEXT NOT NULL DEFAULT 'pending'",
        "jd_error": "TEXT NOT NULL DEFAULT ''",
        "jd_rule_checked": "INTEGER NOT NULL DEFAULT 0",
        "jd_rule_checked_at": "TEXT NOT NULL DEFAULT ''",
        "is_blacklisted": "INTEGER NOT NULL DEFAULT 0",
        "company_blacklisted": "INTEGER NOT NULL DEFAULT 0",
        "jd_blacklisted": "INTEGER NOT NULL DEFAULT 0",
        "blacklist_types": "TEXT NOT NULL DEFAULT '[]'",
        "matched_rule_ids": "TEXT NOT NULL DEFAULT '[]'",
        "matched_rule_snapshot": "TEXT NOT NULL DEFAULT '[]'",
        "blacklist_checked_at": "TEXT NOT NULL DEFAULT ''",
        "auto_delivery_blocked": "INTEGER NOT NULL DEFAULT 1",
        "last_greeted_at": "TEXT NOT NULL DEFAULT ''",
    },
    "fetch_pages": {
        "task_id": "TEXT NOT NULL DEFAULT ''",
    },
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """给老库补上后来加的列。新开库/已补过的库都是无操作。"""
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, ddl in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            logger.info("状态库补列：%s.%s", table, name)


def _open(resolved: Path, *, migrate: bool) -> sqlite3.Connection:
    """打开连接并建表/补列；只对默认库做历史迁移。"""
    is_memory = str(resolved) == ":memory:"
    created = False
    if not is_memory:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        created = not resolved.exists()

    conn = sqlite3.connect(str(resolved), timeout=5.0, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        if not is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        _migrate_columns(conn)
        conn.commit()
    except sqlite3.Error:
        conn.close()
        raise

    if not is_memory:
        try:
            os.chmod(resolved, 0o600)
        except OSError:
            pass
        if created:
            logger.info("状态库已建：%s", resolved)

    if migrate and not is_memory and resolved == DEFAULT_DB_PATH:
        with _migrate_lock:
            migrate_legacy(conn, resolved)
    return conn


def doc_get_raw(name: str, path: Path | str | None = None) -> str | None:
    """读 payload 的原始文本（给要自己报 `ValueError` 的调用方用）。"""
    conn = acquire(path)
    row = conn.execute("SELECT payload FROM doc WHERE name = ?", (name,)).fetchone()
    return None if row is None else str(row["payload"])


def doc_get(name: str, path: Path | str | None = None) -> dict[str, Any] | None:
    """读一份单例文档。没有 / payload 坏了 → `None`（调用方按「没有」处理）。"""
    return _decode_payload(doc_get_raw(name, path))


def doc_set(name: str, payload: Mapping[str, Any] | list[Any] | str, path: Path | str | None = None) -> Path:
    """写一份单例文档。`payload` 可以是对象，也可以是已经序列化好的字符串。"""
    resolved = resolve_db_path(path)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    conn = acquire(resolved)
    with conn:
        conn.execute(
            "INSERT INTO doc (name, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (name, text, time.time()),
        )
    return resolved


def doc_delete(name: str, path: Path | str | None = None) -> bool:
    """删一份单例文档；返回「真的删掉了东西」。"""
    conn = acquire(path)
    with conn:
        cur = conn.execute("DELETE FROM doc WHERE name = ?", (name,))
    return cur.rowcount > 0


def _decode_payload(raw: Any) -> dict[str, Any] | None:
    """把 payload 文本解成 dict；坏 JSON / 非对象按「没有」处理。"""
    if raw is None:
        return None
    try:
        data = json.loads(str(raw))
    except ValueError:
        logger.warning("doc.payload 不是合法 JSON，按没有处理")
        return None
    return data if isinstance(data, dict) else None


def migrate_legacy(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    legacy_root: Path | None = None,
) -> dict[str, int]:
    """把历史 JSON 账本 / `jobs.db` 迁进库；幂等，跑第二次是 no-op。

    Args:
        conn: 目标库连接。
        db_path: 目标库路径（打日志用）。
        legacy_root: 旧账本所在的根目录（默认项目根）。测试里指到临时目录。

    Returns:
        各类迁移条数（没迁的键为 0 或省略）。
    """
    root = Path(legacy_root) if legacy_root is not None else PROJECT_ROOT
    gate = _decode_payload(_raw_row(conn, SCHEMA_DOC) or "")
    if gate and gate.get(_MIGRATED_FLAG):
        return {}

    counts = {
        "session": 0,
        "search_filter": 0,
        "stoken": 0,
        "llm_config": 0,
        "resume": 0,
        "analysis": 0,
        "jobs": 0,
        "fetch_pages": 0,
    }
    data_dir = root / "data"

    counts["jobs"], counts["fetch_pages"] = _import_jobs_db(conn, root / "jobs.db")

    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, src in (
            (DOC_SESSION, root / "session.json"),
            (DOC_SEARCH_FILTER, root / "search_filter.json"),
            (DOC_STOKEN, root / "stoken.json"),
            (DOC_LLM_CONFIG, data_dir / "llm_config.json"),
        ):
            if _raw_row(conn, name) is not None or not src.exists():
                continue
            try:
                text = src.read_text(encoding="utf-8")
                json.loads(text)
            except (OSError, ValueError) as exc:
                logger.warning("迁移跳过 %s：%s", src, exc)
                continue
            conn.execute(
                "INSERT INTO doc (name, payload, updated_at) VALUES (?, ?, ?)",
                (name, text, src.stat().st_mtime),
            )
            counts[name] = 1
            _unlink_quiet(src)

        counts["resume"] = _import_resumes(conn, data_dir / "resumes")
        counts["analysis"] = _import_analyses(conn, data_dir / "analyses")

        conn.execute(
            "INSERT INTO doc (name, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (
                SCHEMA_DOC,
                json.dumps({_MIGRATED_FLAG: True, "migrated_at": time.time()}),
                time.time(),
            ),
        )
    except sqlite3.Error:
        conn.rollback()
        raise
    else:
        conn.commit()

    moved = {k: v for k, v in counts.items() if v}
    if moved:
        logger.info("历史账本已迁入 %s：%s", db_path, moved)
    return counts


def _raw_row(conn: sqlite3.Connection, name: str) -> str | None:
    """读 `doc` 某行的原始 payload 文本。"""
    row = conn.execute("SELECT payload FROM doc WHERE name = ?", (name,)).fetchone()
    return None if row is None else str(row["payload"])


def _import_resumes(conn: sqlite3.Connection, folder: Path) -> int:
    """把 `data/resumes` 下的简历文件迁进 `resume` 表；返回迁入条数。"""
    if not folder.is_dir():
        return 0
    n = 0
    for meta_file in sorted(folder.glob("rs_*.json"), reverse=True):
        rid = meta_file.stem
        md_file = folder / f"{rid}.md"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            raw = md_file.read_text(encoding="utf-8") if md_file.exists() else ""
        except (OSError, ValueError) as exc:
            logger.warning("迁移跳过 %s：%s", meta_file, exc)
            continue
        if not isinstance(meta, dict):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO resume (resume_id, title, source_name, created_at, "
            "content_md, meta) VALUES (?, ?, ?, ?, ?, ?)",
            (
                rid,
                str(meta.get("title") or ""),
                str(meta.get("source_name") or meta.get("title") or f"{rid}.md"),
                float(meta.get("created_at") or meta_file.stat().st_mtime),
                raw,
                json.dumps(meta, ensure_ascii=False),
            ),
        )
        n += 1
        _unlink_quiet(md_file)
        _unlink_quiet(meta_file)
    if n:
        _rmdir_quiet(folder)
    return n


def _import_analyses(conn: sqlite3.Connection, folder: Path) -> int:
    """把 `data/analyses` 下的分析 JSON 迁进 `analysis` 表；返回迁入条数。"""
    if not folder.is_dir():
        return 0
    n = 0
    for src in sorted(folder.glob("an_*.json"), reverse=True):
        try:
            payload = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("迁移跳过 %s：%s", src, exc)
            continue
        if not isinstance(payload, dict):
            continue
        matches = payload.get("matches") or []
        scores = [m.get("match_score") for m in matches if isinstance(m, dict)]
        scores = [s for s in scores if isinstance(s, (int, float))]
        llm = payload.get("llm") or {}
        conn.execute(
            "INSERT OR IGNORE INTO analysis (analysis_id, resume_id, resume_title, "
            "created_at, status, model, base_url, job_count, top_score, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(payload.get("analysis_id") or src.stem),
                str(payload.get("resume_id") or ""),
                str(payload.get("resume_title") or ""),
                float(payload.get("created_at") or src.stat().st_mtime),
                str(payload.get("status") or "done"),
                str(llm.get("model") or ""),
                str(llm.get("base_url") or ""),
                len(matches),
                float(max(scores) if scores else 0.0),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        n += 1
        _unlink_quiet(src)
    if n:
        _rmdir_quiet(folder)
    return n


def _import_jobs_db(conn: sqlite3.Connection, legacy: Path) -> tuple[int, int]:
    """把旧 `jobs.db` 迁进库；返回 `(职位数, 抓取页数)`。目标库已有职位时只挪走旧文件。"""
    if not legacy.exists():
        return 0, 0
    have = conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
    if have:
        _rename_quiet(legacy, legacy.with_name(legacy.name + ".migrated"))
        return 0, 0

    alias = "legacy_jobs"
    jobs = pages = 0
    try:
        conn.execute(f"ATTACH DATABASE ? AS {alias}", (str(legacy),))
        jobs = int(conn.execute(f"SELECT count(*) AS n FROM {alias}.jobs").fetchone()["n"])
        pages = int(conn.execute(f"SELECT count(*) AS n FROM {alias}.fetch_pages").fetchone()["n"])
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO jobs (encrypt_job_id, job_name, brand_name, location, "
            "salary_desc, job_experience, job_degree, brand_industry, brand_scale_name, "
            "city_name, area_district, business_district, brand_stage_name, job_labels, "
            "skills, welfare_list, boss_name, boss_title, expect_id, job_type, "
            "job_valid_status, security_id, lid, page, raw_json, fetched_at) "
            "SELECT encrypt_job_id, COALESCE(job_name, ''), COALESCE(brand_name, ''), "
            "COALESCE(location, ''), COALESCE(salary_desc, ''), COALESCE(job_experience, ''), "
            "COALESCE(job_degree, ''), COALESCE(brand_industry, ''), COALESCE(brand_scale_name, ''), "
            "COALESCE(city_name, ''), COALESCE(area_district, ''), COALESCE(business_district, ''), "
            "COALESCE(brand_stage_name, ''), COALESCE(job_labels, '[]'), COALESCE(skills, '[]'), "
            "COALESCE(welfare_list, '[]'), COALESCE(boss_name, ''), COALESCE(boss_title, ''), "
            "COALESCE(expect_id, ''), COALESCE(job_type, 0), COALESCE(job_valid_status, 1), "
            "COALESCE(security_id, ''), COALESCE(lid, ''), COALESCE(page, 0), "
            "COALESCE(raw_json, ''), COALESCE(fetched_at, '') FROM legacy_jobs.jobs"
        )
        conn.execute(
            "INSERT INTO fetch_pages (page, raw_count, kept_count, dropped_count, "
            "inserted_count, updated_count, has_more, note, fetched_at) "
            "SELECT page, COALESCE(raw_count, 0), COALESCE(kept_count, 0), "
            "COALESCE(dropped_count, 0), COALESCE(inserted_count, 0), "
            "COALESCE(updated_count, 0), COALESCE(has_more, 0), COALESCE(note, ''), "
            "COALESCE(fetched_at, '') FROM legacy_jobs.fetch_pages"
        )
        got_jobs = int(conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"])
        got_pages = int(conn.execute("SELECT count(*) AS n FROM fetch_pages").fetchone()["n"])
        if got_jobs != jobs or got_pages != pages:
            conn.rollback()
            raise sqlite3.DatabaseError(
                f"jobs.db 迁移条数对不上：源 {jobs}/{pages} → 库 {got_jobs}/{got_pages}"
            )
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning("迁移 jobs.db 失败：%s", exc)
        jobs = pages = 0
    finally:
        try:
            conn.execute(f"DETACH DATABASE {alias}")
        except sqlite3.Error:
            pass

    if jobs:
        _rename_quiet(legacy, legacy.with_name(legacy.name + ".migrated"))
    return jobs, pages


def _unlink_quiet(path: Path) -> None:
    """删文件；失败只 warning。"""
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("删不掉 %s：%s", path, exc)


def _rmdir_quiet(path: Path) -> None:
    """删空目录；还有别的文件就留着。"""
    try:
        path.rmdir()
    except OSError:
        pass


def _rename_quiet(src: Path, dst: Path) -> None:
    """改名（目标已存在则先删）；失败只 warning。"""
    try:
        if dst.exists():
            dst.unlink()
        src.rename(dst)
    except OSError as exc:
        logger.warning("改名失败 %s -> %s：%s", src, dst, exc)


__all__ = [
    "PROJECT_ROOT",
    "DATA_DIR",
    "DEFAULT_DB_PATH",
    "DB_ENV",
    "SCHEMA_DOC",
    "DOC_SESSION",
    "DOC_SEARCH_FILTER",
    "DOC_STOKEN",
    "DOC_LLM_CONFIG",
    "DOC_DELIVER_CONFIG",
    "DOC_APP_SETTINGS",
    "resolve_db_path",
    "db",
    "acquire",
    "connect",
    "close_db",
    "close_all",
    "doc_get",
    "doc_get_raw",
    "doc_set",
    "doc_delete",
    "migrate_legacy",
]
