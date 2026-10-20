# `tests.test_web_deliver` 协议笔记

> 摘自 `tests/test_web_deliver.py` 的 `test_deliver_chat_remind_continues_batch_without_login_hint` 文档字符串。

## 一句话

「开聊提醒」是提示弹窗不是停批信号，更不能叫人去重新登录。

## 细节

实测（2026-10-09）：`friend/add.json` 回 code 1 + chatRemindDialog，
话术是「您今天已与120位BOSS沟通，还剩30次沟通机会哦」——那 30 是**还能再
发的次数**。以前 code 1 被一律当登录态失效，界面提示「请先到登录页重新
登录」；后来又改成整批停手，剩下的 30 次白白浪费。

`JobClient.greet` 里已经做过「模拟点击确认」（cid=1）；确认完还被拦
（这条用例）只记这一条，**继续发剩下的**。
