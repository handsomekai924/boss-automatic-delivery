# `boss_web.services.troubleshoot` 协议笔记

> 摘自 `boss_web/services/troubleshoot.py` 的模块文档字符串 / `#` 注释。

## 一句话

把底层技术异常翻译成人话，外加 Chrome 环境检测。

## 细节

`boss_jobs` 抛的 `StokenError` 是给开发者看的——裸类名、`ws://127.0.0.1:9222/
devtools/browser/<uuid>`、CDP 方法名。普通用户看到这些只会去问「这是啥」。

这一层只做翻译和探测，**不改 `boss_jobs` 一行**：错误从哪里冒出来就在哪里翻
（Web 端走异常处理器，后台线程直接调 `humanize`）。

### `_RULES` 顺序

`(原文里的特征串, 给用户看的话)`，**顺序敏感**：从上往下第一条命中为准，
所以更具体的规则要排在更泛的前面。

⚠️ 「找不到 Chrome」必须排在「BOSS_CHROME_BIN」**前面**：前者那句原文本身就
把环境变量当兜底方案提了一嘴（`…或用 BOSS_CHROME_BIN 指到 chrome…`），
排在后面的话永远轮不到它，用户拿到的提示会变成让他去设环境变量。

### 登录态规则

`boss_jobs.http_from_session` 抛的原文末尾是「先跑 `python -m boss_login
login`」——那是命令行的说法。网页用户该去「登录」页，别把他支去终端。
