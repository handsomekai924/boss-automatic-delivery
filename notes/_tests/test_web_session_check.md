# `tests.test_web_session_check` 协议笔记

> 摘自 `tests/test_web_session_check.py` 的模块文档字符串。

## 一句话

启动时的登录态自检：验真伪、失效清库、断网不动库。

## 细节

规矩只有三条，逐条钉死：

1. **服务端说失效才清库**——`getUserInfo` 回个「系统繁忙」不算，那会被
   `fetch_user_info` 包成 `SessionExpired`，清了就把好凭证抹了。
2. **网络/意外错误不动库**——断网不是登录失效。
3. **自检完成的 Event 无条件置位**——哪怕线程炸了，浏览器也不能被扣住不开。
