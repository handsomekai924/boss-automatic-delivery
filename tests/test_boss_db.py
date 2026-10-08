"""状态库 ``boss_db`` 的单元测试（离线，全部落在临时目录）。"""

from __future__ import annotations

import json
import sqlite3

import pytest

import boss_db


@pytest.fixture(autouse=True)
def _clean_cache():
    boss_db.close_all()
    yield
    boss_db.close_all()


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #


def test_resolve_默认是_data_boss_db():
    import os

    os.environ.pop(boss_db.DB_ENV, None)
    assert boss_db.resolve_db_path() == boss_db.DEFAULT_DB_PATH
    assert boss_db.DEFAULT_DB_PATH.name == "boss.db"
    assert boss_db.DEFAULT_DB_PATH.parent.name == "data"


def test_resolve_认环境变量(tmp_path, monkeypatch):
    monkeypatch.setenv(boss_db.DB_ENV, str(tmp_path / "x.db"))
    assert boss_db.resolve_db_path() == tmp_path / "x.db"


def test_resolve_显式参数优先(tmp_path, monkeypatch):
    monkeypatch.setenv(boss_db.DB_ENV, "from-env.db")
    assert boss_db.resolve_db_path(tmp_path / "explicit.db") == tmp_path / "explicit.db"


def test_resolve_内存库原样透传():
    assert str(boss_db.resolve_db_path(":memory:")) == ":memory:"


# --------------------------------------------------------------------------- #
# 建表 / doc CRUD
# --------------------------------------------------------------------------- #


def test_connect_建出全部表(tmp_path):
    conn = boss_db.connect(tmp_path / "boss.db", migrate=False)
    try:
        names = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert {"doc", "resume", "analysis", "jobs", "fetch_pages"} <= names


def test_doc_往返(tmp_path):
    p = tmp_path / "boss.db"
    assert boss_db.doc_get("session", p) is None
    boss_db.doc_set("session", {"token": "t", "cookies": {"a": "1"}}, p)
    got = boss_db.doc_get("session", p)
    assert got == {"token": "t", "cookies": {"a": "1"}}
    assert boss_db.doc_delete("session", p) is True
    assert boss_db.doc_delete("session", p) is False
    assert boss_db.doc_get("session", p) is None


def test_doc_坏payload按没有处理(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set("session", "{不是 json", p)
    assert boss_db.doc_get("session", p) is None
    # 原文还在，给要自己报错的调用方用
    assert boss_db.doc_get_raw("session", p) == "{不是 json"


def test_doc_raw_可以直接塞字符串(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set("llm_config", '{"model": "m1"}', p)
    assert boss_db.doc_get("llm_config", p) == {"model": "m1"}


# --------------------------------------------------------------------------- #
# 连接缓存
# --------------------------------------------------------------------------- #


def test_内存库共享同一个连接():
    """``JobStore(":memory:")`` 与 doc 助手必须看得见同一份数据。"""
    a = boss_db.acquire(":memory:")
    boss_db.doc_set("stoken", {"token": "x"}, ":memory:")
    b = boss_db.acquire(":memory:")
    assert a is b
    row = b.execute("SELECT payload FROM doc WHERE name='stoken'").fetchone()
    assert json.loads(row["payload"]) == {"token": "x"}


def test_close_all_之后重开(tmp_path):
    p = tmp_path / "boss.db"
    boss_db.doc_set("session", {"a": 1}, p)
    boss_db.close_all()
    assert boss_db.doc_get("session", p) == {"a": 1}


# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #


def _write_legacy_tree(root):
    """伪造一套旧账本（内容随便，够断言就行）。"""
    (root / "data" / "resumes").mkdir(parents=True)
    (root / "data" / "analyses").mkdir(parents=True)
    (root / "session.json").write_text(
        json.dumps({"token": "tok", "cookies": {"wt2": "c"}, "phone_masked": "138****8000"}),
        encoding="utf-8",
    )
    (root / "search_filter.json").write_text(
        json.dumps({"query": "Python", "city": "101280100"}), encoding="utf-8"
    )
    (root / "stoken.json").write_text(
        json.dumps({"token": "s", "minted_at": 1.0, "expires_at": 2.0, "source": "cdp"}),
        encoding="utf-8",
    )
    (root / "data" / "llm_config.json").write_text(
        json.dumps({"api_key": "sk-abc", "base_url": "https://x/v1", "model": "m"}),
        encoding="utf-8",
    )
    (root / "data" / "resumes" / "rs_a.md").write_text("# hi\n正文", encoding="utf-8")
    (root / "data" / "resumes" / "rs_a.json").write_text(
        json.dumps({"resume_id": "rs_a", "title": "a.md", "created_at": 10.0, "skills": ["Py"]}),
        encoding="utf-8",
    )
    (root / "data" / "analyses" / "an_b.json").write_text(
        json.dumps(
            {
                "analysis_id": "an_b",
                "resume_id": "rs_a",
                "created_at": 11.0,
                "status": "done",
                "llm": {"base_url": "https://x/v1", "model": "m"},
                "matches": [{"match_score": 80}, {"match_score": 55}],
            }
        ),
        encoding="utf-8",
    )
    # 迷你 jobs.db
    src = sqlite3.connect(str(root / "jobs.db"))
    src.executescript(
        """
        CREATE TABLE jobs (encrypt_job_id TEXT PRIMARY KEY, job_name TEXT, brand_name TEXT,
            location TEXT, salary_desc TEXT, job_experience TEXT, job_degree TEXT,
            brand_industry TEXT, brand_scale_name TEXT, city_name TEXT, area_district TEXT,
            business_district TEXT, brand_stage_name TEXT, job_labels TEXT, skills TEXT,
            welfare_list TEXT, boss_name TEXT, boss_title TEXT, expect_id TEXT, job_type INT,
            job_valid_status INT, security_id TEXT, lid TEXT, page INT, raw_json TEXT,
            fetched_at TEXT);
        CREATE TABLE fetch_pages (id INTEGER PRIMARY KEY AUTOINCREMENT, page INT,
            raw_count INT, kept_count INT, dropped_count INT, inserted_count INT,
            updated_count INT, has_more INT, note TEXT, fetched_at TEXT);
        """
    )
    src.execute(
        "INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at) VALUES (?,?,?,?)",
        ("e1", "后端", "某厂", "2026-01-01"),
    )
    src.execute(
        "INSERT INTO fetch_pages (page, raw_count, kept_count, dropped_count, "
        "inserted_count, updated_count, has_more, note, fetched_at) "
        "VALUES (1, 2, 1, 1, 1, 0, 1, '', '2026-01-01')"
    )
    src.commit()
    src.close()


def test_migrate_一次性导入(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write_legacy_tree(legacy)

    db_path = tmp_path / "boss.db"
    conn = boss_db.connect(db_path, migrate=False)
    try:
        counts = boss_db.migrate_legacy(conn, db_path, legacy_root=legacy)
    finally:
        conn.close()

    assert counts["session"] == 1
    assert counts["search_filter"] == 1
    assert counts["stoken"] == 1
    assert counts["llm_config"] == 1
    assert counts["resume"] == 1
    assert counts["analysis"] == 1
    assert counts["jobs"] == 1
    assert counts["fetch_pages"] == 1

    # 内容对得上
    assert boss_db.doc_get("session", db_path)["phone_masked"] == "138****8000"
    assert boss_db.doc_get("search_filter", db_path)["query"] == "Python"
    assert boss_db.doc_get("llm_config", db_path)["model"] == "m"

    conn = boss_db.connect(db_path, migrate=False)
    try:
        r = conn.execute("SELECT title, content_md FROM resume").fetchone()
        assert r["title"] == "a.md"
        assert "正文" in r["content_md"]
        a = conn.execute("SELECT job_count, top_score FROM analysis").fetchone()
        assert a["job_count"] == 2
        assert a["top_score"] == 80
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1
    finally:
        conn.close()

    # JSON 账本清掉，jobs.db 改名保留
    assert not (legacy / "session.json").exists()
    assert not (legacy / "search_filter.json").exists()
    assert not (legacy / "stoken.json").exists()
    assert not (legacy / "data" / "llm_config.json").exists()
    assert not (legacy / "data" / "resumes").exists()
    assert not (legacy / "data" / "analyses").exists()
    assert not (legacy / "jobs.db").exists()
    assert (legacy / "jobs.db.migrated").exists()


def test_migrate_幂等(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write_legacy_tree(legacy)
    db_path = tmp_path / "boss.db"

    conn = boss_db.connect(db_path, migrate=False)
    try:
        boss_db.migrate_legacy(conn, db_path, legacy_root=legacy)
        again = boss_db.migrate_legacy(conn, db_path, legacy_root=legacy)
    finally:
        conn.close()

    assert again == {}
    conn = boss_db.connect(db_path, migrate=False)
    try:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM fetch_pages").fetchone()["n"] == 1
    finally:
        conn.close()


def test_migrate_坏文件跳过不挡别的(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "session.json").write_text("{不是 json", encoding="utf-8")
    (legacy / "stoken.json").write_text(
        json.dumps({"token": "s", "minted_at": 1.0, "expires_at": 2.0}), encoding="utf-8"
    )

    db_path = tmp_path / "boss.db"
    conn = boss_db.connect(db_path, migrate=False)
    try:
        counts = boss_db.migrate_legacy(conn, db_path, legacy_root=legacy)
    finally:
        conn.close()

    assert counts["session"] == 0
    assert counts["stoken"] == 1
    # 坏文件留着，好文件搬走
    assert (legacy / "session.json").exists()
    assert not (legacy / "stoken.json").exists()


def test_migrate_目标库已有职位就不搬(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write_legacy_tree(legacy)
    db_path = tmp_path / "boss.db"

    conn = boss_db.connect(db_path, migrate=False)
    try:
        conn.execute(
            "INSERT INTO jobs (encrypt_job_id, job_name, brand_name, fetched_at) "
            "VALUES ('mine','x','y','2026-01-01')"
        )
        conn.commit()
        counts = boss_db.migrate_legacy(conn, db_path, legacy_root=legacy)
    finally:
        conn.close()

    assert counts["jobs"] == 0
    conn = boss_db.connect(db_path, migrate=False)
    try:
        ids = {r["encrypt_job_id"] for r in conn.execute("SELECT encrypt_job_id FROM jobs")}
    finally:
        conn.close()
    assert ids == {"mine"}
    assert (legacy / "jobs.db.migrated").exists()


def test_migrate_坏旧库就不搬职位(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "jobs.db").write_bytes(b"not a sqlite file at all")
    (legacy / "stoken.json").write_text(
        json.dumps({"token": "s", "minted_at": 1.0, "expires_at": 2.0}), encoding="utf-8"
    )

    db_path = tmp_path / "boss.db"
    conn = boss_db.connect(db_path, migrate=False)
    try:
        counts = boss_db.migrate_legacy(conn, db_path, legacy_root=legacy)
    finally:
        conn.close()

    assert counts["jobs"] == 0
    assert counts["stoken"] == 1
