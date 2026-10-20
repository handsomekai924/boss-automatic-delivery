# `boss_filter.tables` 协议笔记

> 摘自 `boss_filter/tables.py` 的模块文档字符串 / `#:` 注释。

## 一句话

筛选项兜底常量表；下表中的文案与 code 与线上接口逐项对齐，不是手抄界面。

## 细节

⚠️ 数据来源

**已实测确认**（2026-09-30 直接打真实站点，Cookie 取自登录会话）：

  GET /wapi/zpgeek/pc/all/filter/conditions.json
      → zpData: {payTypeList, experienceList, salaryList, stageList,
                 scaleList, partTimeList, degreeList, jobTypeList}
      各项均为 `{code, name}`（salaryList 另有 `lowSalary` / `highSalary`）。
  GET /wapi/zpgeek/search/job/hot/city.json
      → zpData.hotCityList: [{code, name}]，共 15 项（含「全国」100010000）。
  GET /wapi/zpCommon/data/city.json
      → 省→市→区三级树，体量约 900KB，不适合写死；兜底时只留热点城市。

**公司行业有独立接口**：`/wapi/zpCommon/data/industry.json` 返回完整的行业树（15 个分组 + 134 个子行业，code 为 `1000xx` 格式）。当前 chunk（`app~2.1a6c0514.js`）里另有 `/wapi/zpgeek/recommend/industry/query.json`（要登录态，返回用户选中的子集）。本表已与 `industry.json` 对齐。兜底时直接用写死表；在线时 `boss_filter.client.load_industries` 会优先拉接口。

公司行业 code 来自 `/wapi/zpCommon/data/industry.json` 拉的真接口码（2026-10-19 实测）。搜索接口 `industry` 参数要的正是这些 `1000xx` 码，不是 HTML `ka="sel-industry-N"` 里的 0-144。
