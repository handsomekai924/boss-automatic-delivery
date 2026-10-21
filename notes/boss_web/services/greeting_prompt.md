# `boss_web.services.greeting_prompt` 协议笔记

> 摘自 `boss_web/services/greeting_prompt.py` 的模块文档字符串 / `#` 注释。

## 一句话

招呼语生成约束的唯一出处：出厂常量 + 用户自定义的取舍。

## 细节

`GREETING_REQUIREMENT` 是**出厂默认**，一字不动。用户可在「系统配置 → 招呼语」
改写，结果存 `boss_web.services.app_settings_store` 的
`greeting_requirement`；空串 = 回落本常量。取值统一走
`resolve_greeting_requirement()`，组装提示词时现读 → 保存即生效，不用重启。

界面只开放这一段约束（字数、语气、禁语）；`match_task` / `resume_parser` /
`resume_analyzer` 的其余提示词继续写死在代码里，不上界面。

### 为什么约束是运行期注入，不是导入期 `.replace()`

`match_task.USER_TEMPLATE` / `GREETING_USER_TEMPLATE` /
`resume_analyzer.USER_TEMPLATE` 里只留 `{greeting_requirement}` 字段，调用点
`template.format(..., greeting_requirement=resolve_greeting_requirement())`。

早期做法是导入期 `.replace("{GREETING_REQUIREMENT}", 常量)` 把文本烤进模板，
调用期再 `.format()`。`str.format()` 不会二次解释替换值里的花括号——但一旦
用户文本里出现 `{` / `}`，烤进模板后就成了 `.format()` 的字段名，直接
`KeyError` / `IndexError`。运行期作为 format 字段传入就绕开了：用户写什么都不炸，
原样进 prompt。
