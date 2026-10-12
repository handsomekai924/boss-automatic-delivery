"""前端契约：静态源码层面的回归检查。

不跑浏览器（真 Chrome E2E 另有 ``tools/wire_search.py`` 手工路径），这里
只读 JS/HTML 源文本，把「文档要求的界面契约」钉死——导航改名、配置迁移、
发送带 mode/确认、API Key 不明文回显等。文本断言跑得快，也挡得住误删。
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "boss_web" / "static"


def _read(rel: str) -> str:
    path = STATIC / rel
    assert path.is_file(), f"缺少静态文件：{rel}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return _read("index.html")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read("js/app.js")


@pytest.fixture(scope="module")
def settings_js() -> str:
    return _read("js/views/settings.js")


@pytest.fixture(scope="module")
def jobs_js() -> str:
    return _read("js/views/jobs.js")


@pytest.fixture(scope="module")
def match_js() -> str:
    return _read("js/views/match.js")


@pytest.fixture(scope="module")
def llm_js() -> str:
    return _read("js/views/llm.js")


# --------------------------------------------------------------------------- #
# 导航 / 路由
# --------------------------------------------------------------------------- #


def test_导航文案是系统配置(index_html: str):
    assert "系统配置" in index_html
    assert 'href="#/settings"' in index_html


def test_首页卡片链到系统配置():
    home = _read("js/views/home.js")
    assert "系统配置" in home
    assert "#/settings" in home


def test_settings路由存在且llm是兼容别名(app_js: str):
    assert '"/settings": renderSettings' in app_js
    # 老入口 /llm 仍可进同一套设置页，不是死链
    assert '"/llm": renderSettings' in app_js
    assert 'return "settings"' in app_js


# --------------------------------------------------------------------------- #
# 系统配置页：两栏 + 未保存确认 + 规则入口
# --------------------------------------------------------------------------- #


def test_系统配置是两栏目录(settings_js: str):
    assert "AI 设置" in settings_js
    assert "采集配置" in settings_js
    assert 'data-tab="ai"' in settings_js
    assert 'data-tab="collection"' in settings_js


def test_切换目录前有未保存确认(settings_js: str):
    assert "未保存" in settings_js
    assert "confirm" in settings_js


def test_规则管理入口齐全(settings_js: str):
    assert "新增公司规则" in settings_js
    assert "新增 JD 规则" in settings_js
    assert "重新检查已入库职位" in settings_js
    assert "/api/blacklist/recheck" in settings_js


def test_规则弹窗说明匹配方式(settings_js: str):
    """JD 规则要告诉用户会检查哪些文本，不是只丢一个输入框。"""
    assert "rule-mode-help" in settings_js or "match_mode" in settings_js
    assert "scope" in settings_js


def test_采集配置含搜索维度与入库策略(settings_js: str):
    for field in ("s-query", "s-city", "s-pageSize", "s-salary", "s-experience"):
        assert field in settings_js, f"采集配置缺搜索维度 {field}"
    for key in ("c-max-pages", "c-fetch-details", "c-store-blacklisted", "c-use-search"):
        assert key in settings_js, f"采集配置缺参数 {key}"
    assert "/api/collection/config" in settings_js


def test_搜索条件提交的是code不是显示名(settings_js: str):
    """下拉的 value 是接口要的 code；提示文案要说明这点。"""
    assert "code" in settings_js


def test_搜索条件多选是点开才弹不是列表框(settings_js: str):
    """原生 `<select multiple size>` 会把选项一直摊开占半屏，不是「点击才显示」。

    多选维度（经验/学历/规模/薪资方式/兼职/融资阶段）走勾选下拉；
    城市/行业数据量大，走级联（见 ``test_城市和行业是级联``）。
    """
    assert "multiple size=" not in settings_js, "多选维度仍是原生列表框"
    assert "multiSelect" in settings_js
    ui = _read("js/ui.js")
    assert "export function multiSelect" in ui
    for field in ("s-experience", "s-degree", "s-scale", "s-payType", "s-partTime", "s-stage"):
        assert field in settings_js, f"缺多选维度 {field}"


def test_城市和行业是级联(settings_js: str):
    """城市树（省→市→区）和行业分组摊成 option 会卡死初始化，改成级联按需渲染。"""
    assert "cascader" in settings_js
    assert "cityPicker" in settings_js
    assert "industryPicker" in settings_js
    assert "cityOptions" not in settings_js, "又把城市树拍平成 option 了"
    ui = _read("js/ui.js")
    assert "export function cascader" in ui
    # 提交值仍是 code：城市单 code，行业是叶子 code 数组
    assert "city: cityPicker.getValue()" in settings_js
    assert "industry: industryPicker.getValue()" in settings_js


def test_级联选完不自动关窗(settings_js: str):
    """点完就收起会让人以为没选上，也改不了主意——留到「确定」或点外面再关。"""
    ui = _read("js/ui.js")
    assert "cascader-ok" in ui
    assert "popApi.close()" in ui
    # 单选点叶子不应再自动收起
    assert "if (!node.children?.length) popApi.close();" not in ui
    # 点节点会重绘列表，e.target 已脱离文档；点外面的判定要用事件路径
    assert "composedPath" in ui
    assert "openPop.contains(e.target)" not in ui


# --------------------------------------------------------------------------- #
# 职位页：搜索编辑器已迁走，只剩摘要 + 跳转
# --------------------------------------------------------------------------- #


def test_职位页不再有搜索条件编辑器(jobs_js: str):
    # 这些 id 现在归「系统配置 / 采集配置」；职位页不该再出现完整搜索表单
    for moved in ("s-query", "s-city", "s-salary", "s-pageSize", "s-experience"):
        assert moved not in jobs_js, f"职位页仍残留搜索字段 {moved}"


def test_职位页有前往采集配置入口(jobs_js: str):
    assert "前往采集配置" in jobs_js
    assert "#/settings" in jobs_js


def test_职位页保留当前条件摘要(jobs_js: str):
    assert "采集配置" in jobs_js  # 摘要区
    assert "search" in jobs_js  # 读当前条件


def test_职位页有黑名单与JD状态标签及筛选(jobs_js: str):
    assert "job-category" in jobs_js
    assert "jd-status-filter" in jobs_js
    assert "JD 规则未检查" in jobs_js or "jd_rule_checked" in jobs_js


# --------------------------------------------------------------------------- #
# 匹配页：安全标签 + 发送契约
# --------------------------------------------------------------------------- #


def test_匹配结果有三类安全标签(match_js: str):
    assert "公司黑名单" in match_js
    assert "JD 关键词黑名单" in match_js or "JD 黑名单" in match_js
    assert "JD 规则未检查" in match_js


def test_缺安全快照要明说(match_js: str):
    assert "缺少匹配时安全快照" in match_js
    # 七个安全字段都要认；缺一个就当旧结果
    for key in (
        "is_blacklisted",
        "company_blacklisted",
        "jd_blacklisted",
        "jd_rule_checked",
        "jd_status",
        "auto_delivery_blocked",
    ):
        assert key in match_js, f"匹配页没识别安全字段 {key}"


def test_一键发送排除受限职位(match_js: str):
    assert "sendAllTargets" in match_js
    assert "sendRestriction" in match_js
    # 一键发送的过滤条件里必须排掉受限项
    assert "!sendRestriction(m)" in match_js


def test_发送请求带mode与确认字段(match_js: str):
    assert "confirmation_token" in match_js
    assert '"mode": "manual"' in match_js or 'mode: "manual"' in match_js
    assert 'mode: "all"' in match_js or '"all"' in match_js
    assert "confirmed: true" in match_js or "confirmed: true" in match_js.replace(" ", "")


def test_发送前提示按匹配时快照执行(match_js: str):
    assert "匹配时" in match_js
    assert "安全快照" in match_js or "匹配时安全快照" in match_js


def test_单条手动发送要二次确认(match_js: str):
    assert "单条手动发送" in match_js
    assert 'mode: "manual"' in match_js or '"mode": "manual"' in match_js


# --------------------------------------------------------------------------- #
# API Key：密码框、掩码、不回填明文
# --------------------------------------------------------------------------- #


def test_APIKey是密码框且留空不改(llm_js: str):
    assert 'type="password"' in llm_js
    assert "留空" in llm_js
    assert "autocomplete=\"off\"" in llm_js or "autocomplete='off'" in llm_js


def test_APIKey保存后不回填输入框(llm_js: str):
    """保存成功要清空密码框；不能把用户刚打的 Key 留在 DOM 里。"""
    assert '$("api-key").value = ""' in llm_js or "api-key\").value = \"\"" in llm_js


def test_后端返回的是掩码Key():
    src = (STATIC.parents[1] / "boss_web" / "services" / "llm_config_store.py").read_text(encoding="utf-8")
    assert "def mask_key" in src
    assert "def masked" in src
    api = (STATIC.parents[1] / "boss_web" / "api" / "llm.py").read_text(encoding="utf-8")
    # 接口层只吐 masked()，不吐原值
    assert "masked()" in api


def test_JS不把Key写进提示框的原值路径(llm_js: str):
    """提示里用的是接口给的掩码字段，不是 input.value。"""
    assert "c.api_key" in llm_js or "saved.api_key" in llm_js
    assert "has_key" in llm_js
