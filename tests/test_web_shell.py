"""壳路由：新壳 `/` 与旧壳 `/legacy/` 的分工（docs §八 共存期）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from boss_web import create_app


@pytest.fixture
def static_tree(tmp_path: Path, monkeypatch) -> Path:
    """造一棵同时有新壳与旧壳的静态树，并把它设成 `STATIC_DIR`。"""
    from boss_web import config as C

    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<html>新壳</html>", encoding="utf-8")
    (tmp_path / "legacy" / "css").mkdir(parents=True)
    (tmp_path / "legacy" / "index.html").write_text("<html>旧壳</html>", encoding="utf-8")
    (tmp_path / "legacy" / "css" / "app.css").write_text("body{}", encoding="utf-8")
    monkeypatch.setattr(C, "STATIC_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client(static_tree: Path) -> TestClient:
    """提供隔离库下的 TestClient。"""
    return TestClient(create_app())


def test_根回新壳(client: TestClient):
    """验 `/` 出新壳（React），不出旧 SPA。"""
    res = client.get("/")
    assert res.status_code == 200
    assert "新壳" in res.text


def test_legacy_入口回旧壳(client: TestClient):
    """验 `/legacy` 与 `/legacy/` 都出旧壳——旧书签不失效。"""
    for path in ("/legacy", "/legacy/"):
        res = client.get(path)
        assert res.status_code == 200
        assert "旧壳" in res.text, path


def test_legacy_下的真文件直接给(client: TestClient):
    """验 `/legacy/css/app.css` 回真文件，不被壳 fallback 吃掉。"""
    res = client.get("/legacy/css/app.css")
    assert res.status_code == 200
    assert res.text == "body{}"


def test_legacy_未知路径回旧壳(client: TestClient):
    """验旧壳的 hash 路由刷新/深链落回旧壳，不会窜到新壳。"""
    res = client.get("/legacy/anything/deep")
    assert res.status_code == 200
    assert "旧壳" in res.text


def test_未知路径回新壳(client: TestClient):
    """验新壳的深链落回新壳（hash 路由靠它刷新不 404）。"""
    res = client.get("/overview/deep")
    assert res.status_code == 200
    assert "新壳" in res.text


def test_没建新壳时退回旧壳(tmp_path: Path, monkeypatch):
    """验前端没构建时根路径退回旧壳，不给用户开白屏（自检会另报缺失）。"""
    from boss_web import config as C

    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "index.html").write_text("<html>旧壳</html>", encoding="utf-8")
    monkeypatch.setattr(C, "STATIC_DIR", tmp_path)
    res = TestClient(create_app()).get("/")
    assert res.status_code == 200
    assert "旧壳" in res.text


def test_legacy_页面的资源引用指向_legacy_目录():
    """验旧壳 index.html 引用 `/static/legacy/…`——挪了目录就得改引用，漏改就是白屏。"""
    source = (
        Path(__file__).resolve().parents[1]
        / "boss_web" / "static" / "legacy" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'href="/static/legacy/css/app.css"' in source
    assert 'src="/static/legacy/js/app.js"' in source
    assert 'href="/static/css/app.css"' not in source
    assert 'src="/static/js/app.js"' not in source
