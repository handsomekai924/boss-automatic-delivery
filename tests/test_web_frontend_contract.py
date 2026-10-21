"""前端契约：静态源码层面的回归检查。

不跑浏览器（真 Chrome E2E 另有 `tools/wire_search.py` 手工路径），这里
只读 JS/HTML 源文本，把「文档要求的界面契约」钉死——导航改名、配置迁移、
发送带 mode/确认、API Key 不明文回显等。文本断言跑得快，也挡得住误删。
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "boss_web" / "static"


def _read(rel: str) -> str:
    """读 `boss_web/static` 下的相对路径文件文本。"""
    path = STATIC / rel
    assert path.is_file(), f"缺少静态文件：{rel}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    """整页 `index.html` 源文本。"""
    return _read("index.html")


@pytest.fixture(scope="module")
def app_js() -> str:
    """路由壳 `js/app.js` 源文本。"""
    return _read("js/app.js")


@pytest.fixture(scope="module")
def settings_js() -> str:
    """系统配置页 `js/views/settings.js` 源文本。"""
    return _read("js/views/settings.js")


@pytest.fixture(scope="module")
def jobs_js() -> str:
    """职位页 `js/views/jobs.js` 源文本。"""
    return _read("js/views/jobs.js")


@pytest.fixture(scope="module")
def match_js() -> str:
    """匹配页壳层加四个子目录视图的契约文本。"""
    return _read("js/views/match.js") + _read("js/views/match_workbench.js") + _read("js/views/match_deliver.js") + _read("js/views/match_crawls.js") + _read("js/views/match_history.js")


@pytest.fixture(scope="module")
def llm_js() -> str:
    """AI 设置脚本 `js/views/llm.js` 源文本。"""
    return _read("js/views/llm.js")


def test_导航文案是系统配置(index_html: str):
    """验导航文案是系统配置。"""
    assert "系统配置" in index_html
    assert 'href="#/settings"' in index_html


def test_首页卡片链到系统配置():
    """验首页卡片链到系统配置。"""
    home = _read("js/views/home.js")
    assert "系统配置" in home
    assert "#/settings" in home


def test_settings路由存在且llm是兼容别名(app_js: str):
    """验 settings 路由存在，且老入口 /llm 是兼容别名不是死链。"""
    assert '"/settings": renderSettings' in app_js
    assert '"/llm": renderSettings' in app_js
    assert 'return "settings"' in app_js


def test_舰桥刷新时广播会话状态(app_js: str):
    """首页「会话」卡片不能只在渲染时取一次——首屏那次失败就一直「离线」。"""
    assert 'new CustomEvent("boss:session"' in app_js


def test_首页订阅会话广播():
    """验首页订阅会话广播。"""
    home = _read("js/views/home.js")
    assert 'addEventListener("boss:session"' in home
    assert 'removeEventListener("boss:session"' in home


def test_系统配置是两栏目录(settings_js: str):
    """验系统配置是两栏目录。"""
    assert "AI 设置" in settings_js
    assert "采集配置" in settings_js
    assert 'data-tab="ai"' in settings_js
    assert 'data-tab="collection"' in settings_js


def test_系统配置有独立招呼语页签(settings_js: str):
    """招呼语提示词走独立页签，不挤进 AI 设置/采集配置。"""
    assert 'data-tab="greeting"' in settings_js
    assert "招呼语" in settings_js
    assert "renderGreetingPrompt" in settings_js


def test_招呼语面板只改这一段约束():
    """面板只开招呼语约束；其他提示词不上界面。

    `maxlength` 钉死 500，和 `app_settings_store.GREETING_REQUIREMENT_MAX_LEN`
    一致——改一处就得改另一处。
    """
    src = _read("js/views/greeting_prompt.js")
    assert "/api/system/greeting-prompt" in src
    assert "恢复默认" in src
    assert 'requirement: ""' in src
    assert 'id="greet-req"' in src
    assert 'maxlength="500"' in src


def test_切换目录前有未保存确认(settings_js: str):
    """验切换目录前有未保存确认。"""
    assert "未保存" in settings_js
    assert "confirm" in settings_js


def test_规则管理入口齐全(settings_js: str):
    """验规则管理入口齐全。"""
    assert "新增公司规则" in settings_js
    assert "新增 JD 规则" in settings_js
    assert "重新检查已入库职位" in settings_js
    assert "/api/blacklist/recheck" in settings_js


def test_规则弹窗说明匹配方式(settings_js: str):
    """JD 规则要告诉用户会检查哪些文本，不是只丢一个输入框。"""
    assert "rule-mode-help" in settings_js or "match_mode" in settings_js
    assert "scope" in settings_js


def test_采集配置含搜索维度与入库策略(settings_js: str):
    """验采集配置含搜索维度与入库策略。"""
    for field in ("s-query", "s-city", "s-pageSize", "s-salary", "s-experience"):
        assert field in settings_js, f"采集配置缺搜索维度 {field}"
    for key in ("c-max-pages", "c-fetch-details", "c-store-blacklisted", "c-source", "c-expect"):
        assert key in settings_js, f"采集配置缺参数 {key}"
    assert "/api/collection/config" in settings_js


def test_采集来源是二选一(settings_js: str):
    """关键词搜索 / 求职期望互斥二态。旧的 use_search 勾选框和「推荐页」档都已移除。"""
    for value in ("search", "expect"):
        assert f'value="{value}"' in settings_js, f"采集来源缺 {value}"
    assert "special_zone" not in settings_js, "「推荐页」special/zone 已移除，不该再出现"
    assert 'name="c-source"' in settings_js
    assert "c-use-search" not in settings_js, "又把 use_search 勾选框加回来了"


def test_求职期望下拉在线拉取(settings_js: str):
    """期望 id 是账号维度的，必须在线拉，不能写死。"""
    assert "/api/collection/expects" in settings_js
    assert "loadExpects" in settings_js


def test_期望流模式下关键词不生效(settings_js: str):
    """站点点期望 tab 时会删掉 query，UI 要跟着置灰/隐藏并提示「期望流不带关键词」。"""
    assert "updateSourceUI" in settings_js
    assert "s-query" in settings_js
    assert "不带关键词" in settings_js


def test_兼职期望切换筛选面板(settings_js: str):
    """兼职态 salary 让位给 payType/partTime（对齐站点 getConfigFields()）。"""
    for field in ("f-salary", "f-payType", "f-partTime", "s-payType", "s-partTime"):
        assert field in settings_js, f"兼职筛选面板缺 {field}"


def test_临时期望发expectInfo不发encryptExpectId(settings_js: str):
    """临时期望（temp_expect）主参数是 expectInfo JSON，键名是站点那五个。

    对齐前端 `getFormData()` 的 temp-expect 分支：`JSON.stringify({expectType,
    position, location, lowSalary, highSalary})`——`location` 是城市 code，
    不是 `locationName`。UI 下拉要有这一项，且写回 `expect_info`。
    临时期望不是列表条目，不该被当成 encryptExpectId。
    """
    assert 'temp.value = "temp"' in settings_js, "期望下拉缺「临时期望」项"
    assert "临时期望（快速添加）" in settings_js
    for key in ("expectType", "position", "location", "lowSalary", "highSalary"):
        assert key in settings_js, f"临时期望 expectInfo 缺键 {key}"
    assert "expect_info" in settings_js, "临时期望要写回 expect_info"
    assert "tempExpectInfo" in settings_js
    assert "expect_kind: \"temp_expect\"" in settings_js


def test_搜索条件提交的是code不是显示名(settings_js: str):
    """下拉的 value 是接口要的 code；提示文案要说明这点。"""
    assert "code" in settings_js


def test_搜索条件多选是点开才弹不是列表框(settings_js: str):
    """原生 `<select multiple size>` 会把选项一直摊开占半屏，不是「点击才显示」。

    多选维度（经验/学历/规模/薪资方式/兼职/融资阶段）走勾选下拉；
    城市/行业数据量大，走级联（见 `test_城市和行业是级联`）。
    """
    assert "multiple size=" not in settings_js, "多选维度仍是原生列表框"
    assert "multiSelect" in settings_js
    ui = _read("js/ui.js")
    assert "export function multiSelect" in ui
    for field in ("s-experience", "s-degree", "s-scale", "s-payType", "s-partTime", "s-stage"):
        assert field in settings_js, f"缺多选维度 {field}"


def test_城市和行业是级联(settings_js: str):
    """城市树（省→市→区）和行业分组摊成 option 会卡死初始化，改成级联按需渲染。

    提交值仍是 code：城市单 code，行业是叶子 code 数组。
    """
    assert "cascader" in settings_js
    assert "cityPicker" in settings_js
    assert "industryPicker" in settings_js
    assert "cityOptions" not in settings_js, "又把城市树拍平成 option 了"
    ui = _read("js/ui.js")
    assert "export function cascader" in ui
    assert "city: cityPicker.getValue()" in settings_js
    assert "industry: industryPicker.getValue()" in settings_js


def test_级联选完不自动关窗(settings_js: str):
    """点完就收起会让人以为没选上，也改不了主意——留到「确定」或点外面再关。

    单选点叶子不应再自动收起；点节点会重绘列表，e.target 已脱离文档，
    点外面的判定要用事件路径。
    """
    ui = _read("js/ui.js")
    assert "cascader-ok" in ui
    assert "popApi.close()" in ui
    assert "if (!node.children?.length) popApi.close();" not in ui
    assert "composedPath" in ui
    assert "openPop.contains(e.target)" not in ui


def test_城市级联不许选省级(settings_js: str):
    """城市树顶层是省（如「广东」101280000），搜索接口只吃市 / 区 code。

    点省只展开下一级不取值；setValue / setNodes 撞见省级 code 也要丢掉，
    免得旧配置把 101280000 原样发出去。直辖市两层同码（北京 101010100）仍可选。
    """
    ui = _read("js/ui.js")
    assert "colIndex === 0 && node.children?.length" in ui, "点省级仍在取值"
    assert "isSelectableCode" in ui, "setValue 没挡省级 code"
    assert "顶层有子级 = 省级分组" in ui


def test_职位页不再有搜索条件编辑器(jobs_js: str):
    """验职位页不再有搜索条件编辑器，这些 id 现在归「系统配置 / 采集配置」。"""
    for moved in ("s-query", "s-city", "s-salary", "s-pageSize", "s-experience"):
        assert moved not in jobs_js, f"职位页仍残留搜索字段 {moved}"


def test_职位页有前往采集配置入口(jobs_js: str):
    """验职位页有前往采集配置入口。"""
    assert "前往采集配置" in jobs_js
    assert "#/settings" in jobs_js


def test_职位页保留当前条件摘要(jobs_js: str):
    """验职位页保留当前条件摘要（摘要区要读当前条件）。"""
    assert "采集配置" in jobs_js
    assert "search" in jobs_js


def test_职位页摘要说清采集来源(jobs_js: str):
    """期望流要在摘要里点名是哪条流（兼职/推荐/我的期望），不能还写「使用搜索条件」。"""
    assert "expect_kind" in jobs_js
    assert "兼职期望流" in jobs_js
    assert "推荐期望流" in jobs_js
    assert "临时期望流" in jobs_js


def test_职位页摘要不把期望流下的关键词算进去(jobs_js: str):
    """期望流只带筛选维度（from_search 不搬 query），摘要打关键词会看成「期望 + 关键词」。

    stage / jobType 同理，兼职态 salary 被 payType/partTime 顶替。
    """
    assert "if (search.query && !expectMode)" in jobs_js
    assert "if (search.jobType && !expectMode)" in jobs_js
    assert "if (search.salary && !partTime)" in jobs_js


def test_职位页摘要点出期望上下文(jobs_js: str):
    """搜索流 + 期望 = 站点「期望 tab + 输入关键词」，摘要不能只写「使用搜索条件」。"""
    assert "期望上下文" in jobs_js
    assert "search.encryptExpectId" in jobs_js


def test_职位页有黑名单与JD状态标签及筛选(jobs_js: str):
    """验职位页有黑名单与 JD 状态标签及筛选。"""
    assert "job-category" in jobs_js
    assert "jd-status-filter" in jobs_js
    assert "JD 规则未检查" in jobs_js or "jd_rule_checked" in jobs_js


def test_匹配结果有三类安全标签(match_js: str):
    """验匹配结果有三类安全标签。"""
    assert "公司黑名单" in match_js
    assert "JD 关键词黑名单" in match_js or "JD 黑名单" in match_js
    assert "JD 规则未检查" in match_js


def test_缺安全快照要明说(match_js: str):
    """验缺安全快照要明说；安全字段都要认，缺一个就当旧结果。"""
    assert "缺少匹配时安全快照" in match_js
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
    """验一键发送的过滤条件里必须排掉受限项。"""
    assert "sendAllTargets" in match_js
    assert "sendRestriction" in match_js
    assert "!sendRestriction(m)" in match_js


def test_发送请求带mode与确认字段(match_js: str):
    """验发送请求带 mode 与确认字段。"""
    assert "confirmation_token" in match_js
    assert '"mode": "manual"' in match_js or 'mode: "manual"' in match_js
    assert 'mode: "all"' in match_js or '"all"' in match_js
    assert "confirmed: true" in match_js or "confirmed: true" in match_js.replace(" ", "")


def test_发送前提示按匹配时快照执行(match_js: str):
    """验发送前提示按匹配时快照执行。"""
    assert "匹配时" in match_js
    assert "安全快照" in match_js or "匹配时安全快照" in match_js


def test_单条手动发送要二次确认(match_js: str):
    """验单条手动发送要二次确认。"""
    assert "单条手动发送" in match_js
    assert 'mode: "manual"' in match_js or '"mode": "manual"' in match_js


def test_APIKey是密码框且留空不改(llm_js: str):
    """验 API Key 是密码框且留空不改。"""
    assert 'type="password"' in llm_js
    assert "留空" in llm_js
    assert "autocomplete=\"off\"" in llm_js or "autocomplete='off'" in llm_js


def test_APIKey保存后不回填输入框(llm_js: str):
    """保存成功要清空密码框；不能把用户刚打的 Key 留在 DOM 里。"""
    assert '$("api-key").value = ""' in llm_js or "api-key\").value = \"\"" in llm_js


def test_后端返回的是掩码Key():
    """验后端返回的是掩码 Key，接口层只吐 masked()，不吐原值。"""
    src = (STATIC.parents[1] / "boss_web" / "services" / "llm_config_store.py").read_text(encoding="utf-8")
    assert "def mask_key" in src
    assert "def masked" in src
    api = (STATIC.parents[1] / "boss_web" / "api" / "llm.py").read_text(encoding="utf-8")
    assert "masked()" in api


def test_JS不把Key写进提示框的原值路径(llm_js: str):
    """提示里用的是接口给的掩码字段，不是 input.value。"""
    assert "c.api_key" in llm_js or "saved.api_key" in llm_js
    assert "has_key" in llm_js


def test_开页面就做版本检测(app_js: str):
    """验开页面就做版本检测。"""
    assert "maybePromptUpdate" in app_js


def test_更新弹窗三个出口都要在():
    """验更新弹窗三个出口都要在。"""
    update_js = _read("js/update.js")
    assert "忽略此版本" in update_js
    assert "取消" in update_js
    assert "前往 Gitee 下载" in update_js
    assert "前往 GitHub 下载" in update_js


def test_只点取消不落任何记忆():
    """「取消 / Esc / 点外面」都不能调忽略接口，否则下次就不弹了。"""
    update_js = _read("js/update.js")
    assert 'act === "cancel"' in update_js
    cancel_block = update_js.split('act === "cancel"', 1)[1].split("if (act ===", 1)[0]
    assert "update/ignore" not in cancel_block
    assert "onClose" in update_js


def test_忽略此版本走后端接口记住():
    """验忽略此版本走后端接口记住，忽略的是「刚弹出来的那一版」，不是硬编码。"""
    update_js = _read("js/update.js")
    assert "/api/system/update/ignore" in update_js
    assert "status.latest" in update_js


def test_弹窗只在_should_prompt_时出现():
    """验弹窗只在 should_prompt 时出现。"""
    update_js = _read("js/update.js")
    assert "should_prompt" in update_js


def test_版本判断只查_gitee():
    """下载入口可以有 GitHub，但版本比较的数据源不能混进 GitHub；弹窗上两个下载入口都要在。"""
    update_js = _read("js/update.js")
    src = (STATIC.parents[1] / "boss_web" / "services" / "update_check.py").read_text(encoding="utf-8")
    assert "gitee.com/api/v5/repos" in src
    assert "api.github.com" not in src
    assert "github.com/repos" not in src
    assert "gitee_url" in update_js
    assert "github_url" in update_js


def test_立即更新按钮只在能自更新时露出():
    """验立即更新按钮只在能自更新时露出。"""
    update_js = _read("js/update.js")
    assert "立即更新" in update_js
    assert "can_self_update" in update_js
    assert "zip_asset" in update_js


def test_立即更新走_apply_接口():
    """验立即更新走 apply 接口。"""
    update_js = _read("js/update.js")
    assert "/api/system/update/apply" in update_js


def test_更新进度走_ws():
    """验更新进度走 WS。"""
    update_js = _read("js/update.js")
    assert "/api/system/update/ws" in update_js
    assert "WebSocket" in update_js


def test_更新完成后只刷新不关标签页():
    """验更新完成后只刷新不关标签页。

    用户自己开的标签页 history.length === 1，window.close 照关不误；
    重启的 exe 带 --no-browser 不会再开一个，关掉就等于页面全丢。
    源码注释会用 window.close 字样写明为什么不能用，断言前先摘掉再看有没有真调用。
    回执是进程内缓存的，不记标记就会「看到回执 → 刷新 → 又看到回执」无限刷；
    存不进 sessionStorage 时要让用户手动 F5，别刷不完。
    """
    update_js = _read("js/update.js")
    code_only = update_js.replace("``window.close()``", "")
    assert "tryCloseTab" not in update_js
    assert "window.close(" not in code_only
    assert "location.reload()" in update_js
    assert "sessionStorage" in update_js
    assert "DONE_SEEN_KEY" in update_js
    assert "立即刷新" in update_js
    assert "F5" in update_js


def test_换文件期要提醒别关电脑():
    """验换文件期要提醒别关电脑。"""
    update_js = _read("js/update.js")
    assert "请不要关闭电脑" in update_js


def test_更新失败要能看见原因():
    """验更新失败要能看见原因。"""
    update_js = _read("js/update.js")
    assert 'ev.stage === "error"' in update_js
    assert "toast" in update_js


def test_更新进度有进度条():
    """验更新进度有进度条。"""
    update_js = _read("js/update.js")
    css = _read("css/app.css")
    assert 'data-role="bar"' in update_js
    assert ".update-progress" in css


def test_首页环境卡显示当前版本():
    """验首页环境卡显示当前版本。"""
    home = _read("js/views/home.js")
    assert "h.version" in home


def test_系统配置多了版本更新页签(settings_js: str):
    """验系统配置多了版本更新页签，有新版时要在页签上点个小圆点。"""
    assert 'data-tab="update"' in settings_js
    assert "版本更新" in settings_js
    assert "settings-tab-dot" in settings_js, "有新版时要在页签上点个小圆点"
    assert "renderSettingsUpdate" in settings_js


def test_圆点只在有新版且未忽略时亮():
    """验圆点只在有新版且未忽略时亮。

    圆点默认是 hidden 的；`display: inline-block` 会把 UA 的 `[hidden]` 压掉，
    那样圆点就一直挂着。判断也要两个字段都看：`should_prompt` 单独一个够用，
    但 `has_update` 点明「有新版」这个前提，免得后端字段语义漂移时跟着误导。
    已忽略的版本不亮：should_prompt 后端已含 !suppressed，前端别再拿 ignored 反卡。
    """
    settings_js = _read("js/views/settings.js")
    body = _fn_body(settings_js, "onUpdateDot")
    assert "has_update" in body
    assert "should_prompt" in body
    assert "dot.hidden" in body
    assert "ignored" not in body, "忽略水位线归后端 should_prompt，前端别再卡一道"
    css = _read("css/app.css")
    assert "[hidden]" in css, "hidden 必须压得住组件自己的 display"
    assert "display: none !important" in css


def test_圆点一直挂着不靠弹窗续命():
    """验关掉弹窗后圆点仍按缓存一直亮着。

    关掉弹窗 ≠ 忽略——只要还有没被忽略的新版，圆点就得一直亮。
    只靠 60s 一轮的 `boss:update` 事件是不够的：关完弹窗进设置页那一分钟里
    没有事件，圆点就灭了，看着像「关掉就没提示」。所以检测结果要进缓存，
    页面一挂载就按缓存画上。
    """
    update_js = _read("js/update.js")
    settings_js = _read("js/views/settings.js")
    assert "lastStatus" in update_js
    assert "export function applyUpdateStatus" in update_js
    assert "export function peekUpdateStatus" in update_js
    assert "applyUpdateStatus(status)" in update_js
    assert "peekUpdateStatus()" in settings_js
    body = _fn_body(settings_js, "renderSettings") if "function renderSettings(" in settings_js else settings_js
    assert "peekUpdateStatus()" in body, "挂载时就要按缓存定圆点"
    modal_src = update_js.split("function showUpdateModal(", 1)[1]
    cancel_block = modal_src.split('act === "cancel"', 1)[1].split("if (act ===", 1)[0]
    assert "applyUpdateStatus" not in cancel_block, "关掉弹窗不该改圆点状态"
    assert "should_prompt: false" not in cancel_block
    ignore_block = modal_src.split('act === "ignore"', 1)[1].split("if (act ===", 1)[0]
    assert "applyUpdateStatus" in ignore_block
    assert "should_prompt: false" in ignore_block


def test_版本更新页签有日志接口和复制诊断():
    """验版本更新页签有日志接口和复制诊断。"""
    src = _read("js/views/settings_update.js")
    assert "/api/system/update/log" in src
    assert "复制诊断信息" in src
    assert "navigator.clipboard" in src
    assert "update.log" in src and "progress.log" in src
    assert "install_mode" in src
    assert "escapeHtml" in src, "日志/诊断文本进 HTML 前必须转义"
    assert "检查更新" in src
    assert "刷新日志" in src


def test_版本更新页签可开更新窗口():
    """验版本更新页签可开更新窗口。"""
    src = _read("js/views/settings_update.js")
    assert "openUpdateDialog" in src
    assert "立即更新" in src


def test_后台轻提示不重复弹窗():
    """挂机检测只 toast + 圆点，绝不叠 modal。"""
    update_js = _read("js/update.js")
    body = _fn_body(update_js, "startUpdateWatcher")
    assert "toast" in body
    assert "发现新版本" in body
    assert "applyUpdateStatus" in body
    assert "should_prompt" in body, "忽略水位线要压住 toast"
    assert "showUpdateModal(" not in body, "watcher 不许再弹窗"
    assert "openUpdateDialog(" not in body, "watcher 不许再弹窗"


def test_首屏弹窗保留且只弹一次():
    """验首屏弹窗保留且只弹一次。"""
    update_js = _read("js/update.js")
    body = _fn_body(update_js, "maybePromptUpdate")
    assert "showUpdateModal" in body
    assert "should_prompt" in body
    assert "markNotified" in body, "弹过之后要记一笔，watcher 别再 toast 一遍"


def test_app启动后台轻提示(app_js: str):
    """验 app 启动后台轻提示。"""
    assert "startUpdateWatcher" in app_js


def _fn_body(src: str, name: str) -> str:
    """抠出 `function <name>(...) { ... }` 的函数体（一层大括号配对）。"""
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"找不到函数 {name}"
    brace = src.find("{", start)
    assert brace >= 0
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace : i + 1]
    raise AssertionError(f"函数 {name} 的大括号没配对")


def test_登录分派按任务类型不按页签():
    """applyTask 必须按 task.kind 分派——两条流的 status 字符串是重叠的。

    共用一个分派器就会串台：扫码任务一启动是 pending，落进短信的分支就
    goStep(2) 把「滑块」那格点亮了，短信进度无端前进。
    分派器本身只做转交，不许直接动界面进度。
    """
    login = _read("js/views/login.js")
    body = _fn_body(login, "applyTask")
    assert "task.kind" in body
    assert "applyQrTask" in body and "applySmsTask" in body
    assert "goStep(" not in body


def test_扫码任务不推短信步骤条():
    """验扫码任务不推短信步骤条。"""
    login = _read("js/views/login.js")
    body = _fn_body(login, "applyQrTask")
    assert "goStep(" not in body, "扫码任务的处理不该碰短信的 4 步轨"
    assert "showQrPane(" in body


def test_短信任务不推扫码面板():
    """验短信任务不推扫码面板。"""
    login = _read("js/views/login.js")
    body = _fn_body(login, "applySmsTask")
    assert "showQrPane(" not in body, "短信任务的处理不该碰扫码面板"
    assert "goStep(" in body


def test_后端任务快照带kind():
    """验后端任务快照带 kind。"""
    src = (STATIC.parents[1] / "boss_web" / "services" / "login_task.py").read_text(encoding="utf-8")
    assert '"kind": self.kind' in src
    assert 'kind="qr"' in src


def test_轮询处理不许抢页签():
    """applyTask / applyQrTask 里调 setMode，页签就会被 900ms 的轮询拽回去。

    点「短信登录」看起来没反应，就是这么来的：处理函数每秒强制切回扫码页签。
    页签只听用户的（生成二维码 / 页签点击 / 页内切回接任务），状态机只管面板内容。
    """
    login = _read("js/views/login.js")
    for fn in ("applyTask", "applyQrTask", "applySmsTask"):
        body = _fn_body(login, fn)
        assert "setMode(" not in body, f"{fn} 不许切页签，页签归用户"


def test_切到短信就放弃扫码():
    """切页签 = 换一条登录流，旧的当场作废——同一时刻只跑一条。

    二维码留着只会挡住「发送验证码」（后端 409），用户得能重新生成一张。
    旧码作废，下次生成必须重取图。
    """
    login = _read("js/views/login.js")
    body = _fn_body(login, "switchFlow")
    assert "abandonLogin" in body, "切页签必须作废旧的那条登录流"
    assert "setMode(" in body
    assert "qr-idle" in body, "作废后二维码面板要归位，逼用户重新生成"
    assert "qrRevision" in body
