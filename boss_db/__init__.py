"""统一状态库（SQLite）。

以前登录态、搜索条件、stoken、LLM 配置、简历、分析各自躺在 ``*.json`` 里，
职位另有一份 ``jobs.db``。现在**只有一份库** ``data/boss.db``：

``doc``
    单例文档（会话 / 搜索条件 / stoken / LLM 配置）。它们本身就是「一份文档」，
    字段由各模块自己的 dataclass 管，所以整包存 JSON，不拆列。
    payload 就是当年那个文件的正文，迁移 = 把文本搬进一行。

``resume`` / ``analysis``
    实体集合，有 CRUD、要按时间排序、要按 id 删——真正的表。
    嵌套字段（章节、匹配结果）整包进 JSON 列，和 ``jobs.job_labels`` 一个惯例。

``jobs`` / ``fetch_pages``
    职位与抓取流水，DDL 从 :mod:`boss_jobs.store` 原样搬来。

路径覆盖顺序：显式 ``path`` → 环境变量 ``BOSS_DB`` → ``data/boss.db``。
用户点名的 JSON 文件读写不走这里（``boss_filter export --out``、
``search_filter_from_file`` 那类）。
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

#: 项目根（``F:\boss``），与各包自己的 ``PROJECT_ROOT`` 同源
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: 运行时数据目录
DATA_DIR: Final[Path] = PROJECT_ROOT / "data"

#: 默认状态库
DEFAULT_DB_PATH: Final[Path] = DATA_DIR / "boss.db"

#: 覆盖库路径的环境变量
DB_ENV: Final[str] = "BOSS_DB"

#: ``doc`` 表里存迁移闸门的行名
SCHEMA_DOC: Final[str] = "schema"

#: 单例文档的名字
DOC_SESSION: Final[str] = "session"
DOC_SEARCH_FILTER: Final[str] = "search_filter"
DOC_STOKEN: Final[str] = "stoken"
DOC_LLM_CONFIG: Final[str] = "llm_config"

#: 迁移闸门字段
_MIGRATED_FLAG: Final[str] = "legacy_imported"

#: 已废弃的环境变量（设了就提醒一声，不认）
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
    detail_fetched_at  TEXT NOT NULL DEFAULT ''
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


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #


def resolve_db_path(path: Path | str | None = None) -> Path:
    """显式 ``path`` → ``BOSS_DB`` → ``data/boss.db``。

    ``":memory:"`` 原样透传（测试用内存库）。
    调用时才解析，绝不把默认值冻在签名里——这样测试只改一个环境变量就能全局隔离。
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


# --------------------------------------------------------------------------- #
# 连接（进程内缓存：":memory:" 必须共享，文件库也省得反复开）
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_conns: dict[str, sqlite3.Connection] = {}
#: 迁移要串行跑：web 线程池会并发 ``connect`` 同一个默认库
_migrate_lock = threading.Lock()


@contextmanager
def db(path: Path | str | None = None, *, migrate: bool = True) -> Iterator[sqlite3.Connection]:
    """借一个连接，用完还回缓存（不 close）。

    连接是 ``check_same_thread=False`` 的——FastAPI 的线程池会拿它。
    """
    conn = acquire(path, migrate=migrate)
    try:
        yield conn
    finally:
        pass  # 连接留在缓存里


def acquire(path: Path | str | None = None, *, migrate: bool = True) -> sqlite3.Connection:
    """拿连接（缓存命中就不重开）。调用方不用 close。"""
    resolved = resolve_db_path(path)
    key = str(resolved)
    with _lock:
        conn = _conns.get(key)
        if conn is not None:
            return conn
        conn = _open(resolved, migrate=migrate)
        _conns[key] = conn
        return conn


def connect(path: Path | str | None = None, *, migrate: bool = True) -> sqlite3.Connection:
    """新开一个连接（调用方负责 close）。给想独占事务的场合用。"""
    return _open(resolve_db_path(path), migrate=migrate)


def close_db(path: Path | str | None = None) -> None:
    """丢掉缓存的连接（测试里让 ``tmp_path`` 能被清理掉）。"""
    key = str(resolve_db_path(path))
    with _lock:
        conn = _conns.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def close_all() -> None:
    """把缓存里所有连接关掉（测试收尾用）。"""
    with _lock:
        conns = list(_conns.values())
        _conns.clear()
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error:
            pass


#: 补列迁移表：``表名 → {列名: 建列 DDL 片段}``。``CREATE TABLE IF NOT EXISTS``
#: 对已存在的表不会补列，老库升级只能靠 ALTER；PRAGMA 查缺再补，幂等。
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "jobs": {
        "job_desc": "TEXT NOT NULL DEFAULT ''",
        "detail_fetched_at": "TEXT NOT NULL DEFAULT ''",
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
        except OSError:  # Windows 上可能不认，无所谓
            pass
        if created:
            logger.info("状态库已建：%s", resolved)

    # 只认默认库：测试/自定义库路径下绝不会去动项目根那几个真账本
    if migrate and not is_memory and resolved == DEFAULT_DB_PATH:
        with _migrate_lock:
            migrate_legacy(conn, resolved)
    return conn


# --------------------------------------------------------------------------- #
# 单例文档
# --------------------------------------------------------------------------- #


def doc_get_raw(name: str, path: Path | str | None = None) -> str | None:
    """读 payload 的原始文本（给要自己报 ``ValueError`` 的调用方用）。"""
    conn = acquire(path)
    row = conn.execute("SELECT payload FROM doc WHERE name = ?", (name,)).fetchone()
    return None if row is None else str(row["payload"])


def doc_get(name: str, path: Path | str | None = None) -> dict[str, Any] | None:
    """读一份单例文档。没有 / payload 坏了 → ``None``（调用方按「没有」处理）。"""
    return _decode_payload(doc_get_raw(name, path))


def doc_set(name: str, payload: Mapping[str, Any] | list[Any] | str, path: Path | str | None = None) -> Path:
    """写一份单例文档。``payload`` 可以是对象，也可以是已经序列化好的字符串。"""
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
    if raw is None:
        return None
    try:
        data = json.loads(str(raw))
    except ValueError:
        logger.warning("doc.payload 不是合法 JSON，按没有处理")
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# 一次性迁移
# --------------------------------------------------------------------------- #


def migrate_legacy(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    legacy_root: Path | None = None,
) -> dict[str, int]:
    """把历史 JSON 账本 / ``jobs.db`` 迁进库。幂等：跑第二次是 no-op。

    :param legacy_root: 旧账本所在的根目录（默认项目根）。测试里指到临时目录，
        免得把真账本搬走。

    迁成功的 JSON 源文件会删掉；``jobs.db`` 只改名成 ``jobs.db.migrated``，留个后路。
    单条失败只 warning，不挡别的条。
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

    # 职位库单独走一步：ATTACH 的库在事务里 DETACH 会被锁住，必须自己 commit 完再 detach
    counts["jobs"], counts["fetch_pages"] = _import_jobs_db(conn, root / "jobs.db")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # --- 单例文档 ---
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
                json.loads(text)  # 校验一下，坏文件不搬
            except (OSError, ValueError) as exc:
                logger.warning("迁移跳过 %s：%s", src, exc)
                continue
            conn.execute(
                "INSERT INTO doc (name, payload, updated_at) VALUES (?, ?, ?)",
                (name, text, src.stat().st_mtime),
            )
            counts[name] = 1
            _unlink_quiet(src)

        # --- 简历 / 分析 ---
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
    row = conn.execute("SELECT payload FROM doc WHERE name = ?", (name,)).fetchone()
    return None if row is None else str(row["payload"])


def _import_resumes(conn: sqlite3.Connection, folder: Path) -> int:
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
    if not legacy.exists():
        return 0, 0
    # 目标库里已经有职位就别搬了（避免重复），只把旧文件挪开
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
        # 显式列名 + COALESCE：旧库里可能有 NULL，目标列是 NOT NULL DEFAULT ''
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
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("删不掉 %s：%s", path, exc)


def _rmdir_quiet(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass  # 还有别的文件就留着


def _rename_quiet(src: Path, dst: Path) -> None:
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
