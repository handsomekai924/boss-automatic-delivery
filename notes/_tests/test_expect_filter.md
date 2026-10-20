# `tests.test_expect_filter` 协议笔记

> 摘自 `tests/test_expect_filter.py` 的模块文档字符串。

## 一句话

JobExpectFilter 四种 kind 的主参数形状对齐站点 `job~0.0c757050.js` 的 `getFormData()`。

## 细节

对齐站点 chunk `job~0.0c757050.js` 的 `getFormData()` 四分支：

```
kind      期望主参数
recommend city + encryptExpectId + mixExpectType
part_time jobType=1903 + encryptExpectId
expect    encryptExpectId
temp      expectInfo（JSON）
```

验四件事：

1. 四种 kind 的主参数形状
2. 共享筛选维度（城市/薪资/经验/学历/行业/规模）照常进参数
3. **兼职态不传 salary**，只传 payType/partTime
4. `query`/`scene` 等 Y 类键**永远不出现**（前端点期望 tab 就删）
