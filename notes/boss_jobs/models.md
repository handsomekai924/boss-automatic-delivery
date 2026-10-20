# `boss_jobs.models` 协议笔记

> 摘自 `boss_jobs/models.py` 的模块文档字符串 / 文档字符串 / `#` 注释。

## 一句话

职位 DTO 与即时清洗规则；求职期望接口的字段名不对称与 JD 正文落点（实测 2026-10-08 / 2026-10-13）。

## 细节

### 字段对应（todo.md 第四节）

```
===========  ==================  =========================================
todo.md      接口字段            说明
===========  ==================  =========================================
岗位名称     jobName             去多余空白
公司名称     brandName           去多余空白
工作地点     cityName + …        三段拼成「广州·天河区·棠下」，分量另存
薪资待遇     salaryDesc          明文；顺带清掉防爬字体残留
工作经验     jobExperience       缺了再从 jobLabels 里认
学历要求     jobDegree           同上
公司行业     brandIndustry
公司规模     brandScaleName
===========  ==================  =========================================
```

清洗发生在 `Job.from_api` / `clean_page` 里——**每拿到一页就洗一页**，洗完的干净职位才入库，原始 item 原样挂在 `raw_json` 上便于追溯。

### `decode_salary_font`

映射来自 chunk 的 `S=["&#xe031;",…,"&#xe03a;"]`（下标即数字）。JSON 接口回的 `salaryDesc` 已经是明文，这里只处理 HTML 兜底解析和历史脏数据里可能出现的两种写法：`&#xe031;` 与 ``。

### `clean_desc`

`postDescription` 在网页上是富文本，接口回的可能是 HTML 也可能是纯文本，这里先按 HTML 剥标签（`<br>` / 块级收尾当换行），再把连续空行压成一行。**不**用 `clean_text` 那种「全压成单空格」——JD 的换行是有结构的（职责/要求分段），压平了后面匹配页没法读。

### `Job.from_api`

jobExperience/jobDegree 偶发为空，退而从 jobLabels 的前两位认（推荐页的 tag-list 顺序就是「经验 · 学历 · 技能…」）。

### `Job.encrypt_boss_id`

打招呼要用的 `encryptBossId`（招聘方加密 id）。列表接口回的字段里就有，但 todo.md 那张表没列它，所以**不单开列**——需要时从 `raw_json` 现读（见 todo.md 3.1 A1）。

### `Job` 字段注释

- `page`：这条是从第几页捞的（入库后才有意义）。
- `job_desc`：职位描述（JD 正文）。列表接口不回，靠 `JobClient.fetch_job_detail` 逐条补；没抓过就是空串。
- `detail_fetched_at`：详情抓取时间（ISO 风格 `%Y-%m-%d %H:%M:%S`）；空串 = 还没抓过。
- `jd_status`：详情处理状态：pending / collecting / completed / empty / failed / unchecked。
- `is_blacklisted` 等：当前规则评估结果；历史分析另存自己的不可变快照。
- `last_greeted_at`：最近一次「发招呼语」成功时间（`%Y-%m-%d %H:%M:%S`）；空串 = 从未发送。30 天发送冷却的唯一岗位级依据；抓取 UPSERT 不覆盖这一列。

### `ExpectItem` / `_expect_item`

字段名不对称是前端真实行为（chunk `job~0.0c757050.js` `clickItemAction` / `partTimeExpectList`）：`expectList[]` 的 id 叫 `encryptId`，另两类叫 `encryptExpectId`。**实测（2026-10-13，有期望后复测）**：`expectList[]` 响应里真的只有 `encryptId`（数字 id 叫 `id`），`partTimeExpectList[]` 真的只有 `encryptExpectId`（数字 id 叫 `expectId`）。这里仍然两个键名都认——`mixExpect` 那侧还没验到，免得接口侧字段名一变就整批丢。

地点也有两副面孔：`locationName` 是显示名（「佛山」），`location` 是混合期望的 **city code**（前端 `getFormData().city = mixLocation`）。`location_name` 是显示名；`location_code` 是混合期望里的 `location`——前端把它当 **city code** 送进 `getFormData().city`（推荐 tab 定位混合期望用），两者不是一回事，别混。

### `clean_expects`

响应形状（**2026-10-13 本账号有期望后实测**：`expectList` 1 条 / `partTimeExpectList` 4 条 / `mixExpect` 仍 `null`）：

```json
{"code": 0, "zpData": {
    "mixExpect": {"encryptExpectId": "...", "mixExpectType": "...",
                  "positionName": "...", "locationName": "..."},
    "expectList": [{"encryptId": "...", "id": 1098941709,
                    "positionName": "...", "location": 101280800}],
    "partTimeExpectList": [{"encryptExpectId": "...", "expectId": 1174275776,
                            "positionName": "...", "type": 0}]
}}
```

缺字段、类型不对一律按空处理，不抛——账号没配兼职期望是常态。

### `extract_job_desc`

**实测**（2026-10-08）：正文在 `zpData.jobInfo.postDescription`，纯文本（带 `\n`），不是 HTML。顶层 `zpData.postDescription` 没有这格。这里按实测路径优先，再兜几个同义位置，万一以后包一层也不用改。
