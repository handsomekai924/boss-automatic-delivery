"""冻结环境路径解析的单元测试。

打包方案的地基：`bootstrap()` 算错一次，用户数据就会写进临时解包目录。
这里断言全部对着真实路径字符串，而不是「跑通就行」。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from boss_db import DB_ENV
from boss_jobs.cdp_stoken import CHROME_PROFILE_ENV
from boss_web import runtime


@pytest.fixture(autouse=True)
def _env_copy(monkeypatch):
    """用环境副本跑，免得污染别的测试。

    `bootstrap()` 直接写 `os.environ`（不走 monkeypatch），收尾由 monkeypatch 换回原对象。
    """
    monkeypatch.setattr(os, "environ", os.environ.copy())


def _fake_frozen(monkeypatch, tmp_path: Path, *, exe_dir: Path | None = None) -> Path:
    """把进程伪装成打包后的 exe；返回 `_MEIPASS`。"""
    meipass = tmp_path / "_MEI12345"
    meipass.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str((exe_dir or tmp_path / "app") / "BossAutoDelivery.exe"))
    return meipass


def test_resource_root_dev_is_repo_root(monkeypatch):
    """验开发态资源根是仓库根。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = runtime.resource_root()
    assert (root / "boss_web" / "runtime.py").is_file(), f"应是仓库根，实际 {root}"
    assert (root / "boss_web" / "static" / "legacy" / "index.html").is_file()


def test_resource_root_frozen_is_meipass(monkeypatch, tmp_path):
    """验打包态资源根是 `_MEIPASS`。"""
    meipass = _fake_frozen(monkeypatch, tmp_path)
    assert runtime.resource_root() == meipass


def test_executable_dir_frozen_is_exe_parent(monkeypatch, tmp_path):
    """验打包态可执行目录是 exe 父目录。"""
    exe_dir = tmp_path / "Desktop"
    exe_dir.mkdir()
    _fake_frozen(monkeypatch, tmp_path, exe_dir=exe_dir)
    assert runtime.executable_dir() == exe_dir


def test_app_data_dir_dev_follows_repo(monkeypatch):
    """验开发态数据目录跟仓库根走。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert runtime.app_data_dir() == runtime.resource_root() / "data"


def test_app_data_dir_frozen_is_portable(monkeypatch, tmp_path):
    """打包态必须是 exe 同目录——不是 _MEIPASS，那个目录退出即清空。"""
    exe_dir = tmp_path / "Desktop"
    exe_dir.mkdir()
    meipass = _fake_frozen(monkeypatch, tmp_path, exe_dir=exe_dir)
    data = runtime.app_data_dir()
    assert data == exe_dir / "data"
    assert meipass not in data.parents, "绝不能落在解包目录里"


def test_app_data_env_overrides_everything(monkeypatch, tmp_path):
    """验 `APP_DATA_ENV` 优先于其它路径推断。"""
    monkeypatch.setenv(runtime.APP_DATA_ENV, str(tmp_path / "custom"))
    assert runtime.app_data_dir() == tmp_path / "custom"


def test_ensure_writable_uses_folder_when_ok(tmp_path):
    """验目录可写时原样返回，且探针文件删干净。"""
    target = tmp_path / "ok"
    assert runtime.ensure_writable(target) == target
    assert not (target / ".write_test").exists(), "探针文件要删干净"


def test_ensure_writable_falls_back_to_user_dir(monkeypatch, tmp_path):
    """exe 放在只读位置（Program Files）时退到 %LOCALAPPDATA%。

    目标用文件占住，mkdir 必炸。
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))

    blocked = tmp_path / "ProgramFiles" / "data"
    blocked.parent.mkdir()
    blocked.write_text("我是个文件，不是目录", encoding="utf-8")

    assert runtime.ensure_writable(blocked) == tmp_path / "local" / "BossAutoDelivery" / "data"


def test_ensure_writable_falls_back_to_temp(monkeypatch, tmp_path):
    """用户目录也写不了（比如被策略锁死）时退到临时目录。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))

    blocked = tmp_path / "blocked"
    blocked.write_text("文件", encoding="utf-8")
    local = tmp_path / "local" / "BossAutoDelivery" / "data"
    local.parent.mkdir(parents=True)
    local.write_text("也是文件", encoding="utf-8")

    assert runtime.ensure_writable(blocked) == tmp_path / "tmp" / "BossAutoDelivery" / "data"


def test_bootstrap_dev_is_noop(monkeypatch, tmp_path):
    """开发态不许改环境——否则仓库里的 data/ 会被搬走。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv(DB_ENV, raising=False)
    monkeypatch.delenv(CHROME_PROFILE_ENV, raising=False)

    info = runtime.bootstrap()

    assert DB_ENV not in os.environ
    assert CHROME_PROFILE_ENV not in os.environ
    assert info["frozen"] == "0"
    assert info["data_dir"] == str(runtime.resource_root() / "data")


def test_bootstrap_frozen_pins_portable_paths(monkeypatch, tmp_path):
    """核心断言：库和 Chrome profile 都落在 exe 同目录，且与 _MEIPASS 无关。"""
    exe_dir = tmp_path / "Desktop"
    exe_dir.mkdir()
    meipass = _fake_frozen(monkeypatch, tmp_path, exe_dir=exe_dir)
    monkeypatch.delenv(DB_ENV, raising=False)
    monkeypatch.delenv(CHROME_PROFILE_ENV, raising=False)

    info = runtime.bootstrap()

    data = exe_dir / "data"
    assert os.environ[DB_ENV] == str(data / "boss.db")
    assert os.environ[CHROME_PROFILE_ENV] == str(data / "chrome_profile")
    assert info["portable"] == "1"
    assert (data / "logs").is_dir()
    assert str(meipass) not in os.environ[DB_ENV]


def test_bootstrap_respects_existing_env(monkeypatch, tmp_path):
    """用户显式设过的路径优先，不被覆盖。"""
    _fake_frozen(monkeypatch, tmp_path)
    monkeypatch.setenv(DB_ENV, str(tmp_path / "mine.db"))

    runtime.bootstrap()

    assert os.environ[DB_ENV] == str(tmp_path / "mine.db")


def test_bootstrap_marks_non_portable_when_falling_back(monkeypatch, tmp_path):
    """退到用户目录时要如实报告，界面得告诉用户「东西存哪了」。

    exe 目录用文件占住，让它写不了。
    """
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    exe_dir = tmp_path / "ProgramFiles"
    exe_dir.mkdir()
    (exe_dir / "data").write_text("挡路的文件", encoding="utf-8")
    _fake_frozen(monkeypatch, tmp_path, exe_dir=exe_dir)
    monkeypatch.delenv(DB_ENV, raising=False)

    info = runtime.bootstrap()

    assert info["portable"] == "0"
    assert info["data_dir"] == str(tmp_path / "local" / "BossAutoDelivery" / "data")


def test_bootstrap_force_runs_in_dev(monkeypatch, tmp_path):
    """force=True 让开发态也能验证打包路径逻辑，不必真打包。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv(runtime.APP_DATA_ENV, str(tmp_path / "elsewhere"))
    monkeypatch.delenv(DB_ENV, raising=False)
    monkeypatch.delenv(CHROME_PROFILE_ENV, raising=False)

    runtime.bootstrap(force=True)

    assert os.environ[DB_ENV] == str(tmp_path / "elsewhere" / "boss.db")


def test_is_temp_run_flags_zip_preview(monkeypatch, tmp_path):
    """直接从压缩包预览双击：exe 落在 %TEMP% 下，数据会丢。"""
    temp = tmp_path / "tmp"
    exe_dir = temp / "Temp1_abc"
    exe_dir.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    _fake_frozen(monkeypatch, tmp_path, exe_dir=exe_dir)

    assert runtime.is_temp_run() is True


def test_is_temp_run_false_for_normal_run(monkeypatch, tmp_path):
    """验正常目录启动不算临时运行。"""
    exe_dir = tmp_path / "Desktop"
    exe_dir.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    _fake_frozen(monkeypatch, tmp_path, exe_dir=exe_dir)

    assert runtime.is_temp_run() is False


def test_is_temp_run_false_in_dev(monkeypatch):
    """验开发态不算临时运行。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert runtime.is_temp_run() is False
