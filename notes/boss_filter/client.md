# `boss_filter.client` 协议笔记

> 摘自 `boss_filter/client.py` 的模块文档字符串 / 函数文档字符串。

## 一句话

筛选条件的线上接口客户端；调真实 wapi 路径，登录态复用 `boss_login` 落盘的 Cookie。

## 细节

筛选接口本身不强制登录，但带上登录态能拿到定位城市、也更接近浏览器真实流量。

### `parse_conditions_payload`

线上形状（2026-09-30 实测）:

```
zpData: {jobTypeList, salaryList, experienceList, degreeList,
         scaleList, payTypeList, stageList, partTimeList}
```

`search/job/condition.json` 是子集（salary 无 low/high，有的版本没 stage），缺的键按空元组处理，不让它把整次请求拖垮。
