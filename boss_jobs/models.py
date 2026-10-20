"""职位的数据传输对象与即时清洗规则。

对外只暴露 dataclass，屏蔽服务端返回结构的差异。清洗发生在
`Job.from_api` / `clean_page` 里，每拿到一页就洗一页；原始 item 挂在
`raw_json` 上便于追溯。字段对应与实测细节见 `notes/boss_jobs/models.md`。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import config as C

_WS_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    """去掉首尾空白、把连续空白压成一个空格。`None` → 空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _WS_RE.sub(" ", value).strip()


def decode_salary_font(value: Any) -> str:
    """把防爬字体的私有区数字还原成 0-9。

    JSON 接口回的 `salaryDesc` 已是明文，这里只处理 HTML 兜底与历史脏数据。
    """
    text = clean_text(value)
    if not text:
        return ""
    for index, entity in enumerate(C.FONT_DIGIT_ENTITIES):
        text = text.replace(entity, str(index))
        text = text.replace(entity.replace("&#x", "\\u").replace(";", ""), str(index))
    for index, char in enumerate(C.FONT_DIGIT_CHARS):
        text = text.replace(char, str(index))
    return text


def clean_salary(value: Any) -> str:
    """薪资待遇：还原字体数字 + 压空白 + 统一大写 K。"""
    text = _WS_RE.sub(" ", decode_salary_font(value)).strip()
    return text.replace("k", "K").replace("Kk", "K")


def join_location(city: str, district: str, business: str) -> str:
    """三段地点拼成「广州·天河区·棠下」；空段自动跳过。"""
    return "·".join(part for part in (city, district, business) if part)


def as_str_list(value: Any) -> tuple[str, ...]:
    """接口的 list 字段拍成干净字符串元组；非 list 按空处理。"""
    if not isinstance(value, list):
        return ()
    return tuple(clean_text(item) for item in value if clean_text(item))


_TAG_BREAK_RE = re.compile(r"(?i)<\s*(br|/p|/div|/li|/h[1-6])\s*[^>]*>")
_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def clean_desc(value: Any) -> str:
    """职位描述正文：HTML 转纯文本 + 压多余空行。

    保留换行结构（职责/要求分段），不像 `clean_text` 全压成单空格。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    text = _TAG_BREAK_RE.sub("\n", value)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = "\n".join(lines)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


@dataclass(frozen=True)
class Job:
    """一条清洗后的职位。字段分三截：todo.md 那 8 项、主键追溯、接口顺带回的杂项。

    地点分量在拼好的 `location` 之外另存，方便按区筛。

    Attributes:
        location: 工作地点（`cityName` + `areaDistrict` + `businessDistrict` 三段拼好）。
        salary_desc: 薪资待遇；顺带清掉防爬字体残留。
        job_experience: 工作经验要求；接口偶发为空时从 `jobLabels` 前两位认。
        job_degree: 学历要求；同上。
        encrypt_job_id: 职位加密 id，全站唯一，入库主键。
        raw_json: 原始 item，清洗前后可对照。
        lid: 列表页 lid。
        security_id: 详情接口用的 securityId。
        page: 这条是从第几页捞的（入库后才有意义）。
        job_desc: 职位描述（JD 正文）；列表接口不回，靠 `JobClient.fetch_job_detail` 逐条补。
        detail_fetched_at: 详情抓取时间（`%Y-%m-%d %H:%M:%S`）；空串 = 还没抓过。
        jd_status: 详情处理状态：pending / collecting / completed / empty / failed / unchecked。
        is_blacklisted: 当前规则评估结果；历史分析另存自己的不可变快照。
        last_greeted_at: 最近一次「发招呼语」成功时间；30 天冷却的唯一岗位级依据，抓取 UPSERT 不覆盖。
    """

    job_name: str
    brand_name: str
    location: str
    salary_desc: str
    job_experience: str
    job_degree: str
    brand_industry: str
    brand_scale_name: str

    encrypt_job_id: str
    raw_json: str = ""

    city_name: str = ""
    area_district: str = ""
    business_district: str = ""

    brand_stage_name: str = ""
    job_labels: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    welfare_list: tuple[str, ...] = ()
    boss_name: str = ""
    boss_title: str = ""
    expect_id: str = ""
    job_type: int = 0
    job_valid_status: int = 1
    security_id: str = ""
    lid: str = ""

    page: int = 0

    job_desc: str = ""
    detail_fetched_at: str = ""
    jd_status: str = "pending"
    jd_error: str = ""
    jd_rule_checked: bool = False
    jd_rule_checked_at: str = ""
    is_blacklisted: bool = False
    company_blacklisted: bool = False
    jd_blacklisted: bool = False
    blacklist_types: tuple[str, ...] = ()
    matched_rule_ids: tuple[str, ...] = ()
    matched_rule_snapshot: tuple[dict[str, Any], ...] = ()
    blacklist_checked_at: str = ""
    auto_delivery_blocked: bool = True
    last_greeted_at: str = ""

    @classmethod
    def from_api(cls, item: dict[str, Any], *, page: int = 0) -> "Job":
        """从接口的 jobList 元素构造。不负责校验，校验在 `clean_page`。

        Args:
            item: 接口返回的单条职位。
            page: 来源页码。

        Returns:
            构造好的职位。
        """
        if not isinstance(item, dict):
            raise TypeError(f"职位不是对象: {type(item).__name__}")

        city = clean_text(item.get("cityName"))
        district = clean_text(item.get("areaDistrict"))
        business = clean_text(item.get("businessDistrict"))

        experience = clean_text(item.get("jobExperience"))
        degree = clean_text(item.get("jobDegree"))
        labels = as_str_list(item.get("jobLabels"))
        if not experience and labels:
            experience = labels[0]
        if not degree and len(labels) > 1:
            degree = labels[1]

        return cls(
            job_name=clean_text(item.get("jobName")),
            brand_name=clean_text(item.get("brandName")),
            location=join_location(city, district, business),
            salary_desc=clean_salary(item.get("salaryDesc")),
            job_experience=experience,
            job_degree=degree,
            brand_industry=clean_text(item.get("brandIndustry")),
            brand_scale_name=clean_text(item.get("brandScaleName")),
            encrypt_job_id=clean_text(item.get("encryptJobId")),
            raw_json=json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            city_name=city,
            area_district=district,
            business_district=business,
            brand_stage_name=clean_text(item.get("brandStageName")),
            job_labels=labels,
            skills=as_str_list(item.get("skills")),
            welfare_list=as_str_list(item.get("welfareList")),
            boss_name=clean_text(item.get("bossName")),
            boss_title=clean_text(item.get("bossTitle")),
            expect_id=_as_str(item.get("expectId")),
            job_type=_as_int(item.get("jobType")),
            job_valid_status=_as_int(item.get("jobValidStatus"), default=1),
            security_id=clean_text(item.get("securityId")),
            lid=clean_text(item.get("lid")),
            page=page,
        )

    @property
    def is_valid(self) -> bool:
        """能入库的最低标准：有主键、有岗位名、有公司名。"""
        return bool(self.encrypt_job_id and self.job_name and self.brand_name)

    @property
    def encrypt_boss_id(self) -> str:
        """打招呼要用的 `encryptBossId`；不单开列，需要时从 `raw_json` 现读。"""
        if not self.raw_json:
            return ""
        try:
            data = json.loads(self.raw_json)
        except ValueError:
            return ""
        if not isinstance(data, dict):
            return ""
        return clean_text(data.get("encryptBossId"))

    @property
    def summary(self) -> str:
        """一行人读摘要（CLI 用）。"""
        return (
            f"{self.job_name} | {self.brand_name} | {self.salary_desc} | "
            f"{self.job_experience} | {self.job_degree} | {self.location} | "
            f"{self.brand_industry} | {self.brand_scale_name}"
        )

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 友好的 dict（元组转 list）。"""
        data = asdict(self)
        for key in ("job_labels", "skills", "welfare_list"):
            data[key] = list(data[key])
        return data


@dataclass(frozen=True)
class PageResult:
    """一页的清洗结果。

    Attributes:
        page: 页码。
        jobs: 洗后的职位。
        has_more: 接口是否还有下一页。
        raw_count: 原始条数。
        dropped: 被丢掉的原因列表。
        lid: 列表页 lid。
    """

    page: int
    jobs: tuple[Job, ...] = ()
    has_more: bool = False
    raw_count: int = 0
    dropped: tuple[str, ...] = ()
    lid: str = ""

    @property
    def dropped_count(self) -> int:
        """被丢掉的条数。"""
        return self.raw_count - len(self.jobs)

    @property
    def is_empty(self) -> bool:
        """本页是否洗后为空。"""
        return not self.jobs


def clean_page(payload: dict[str, Any], *, page: int) -> PageResult:
    """把一页响应洗成 `PageResult`。

    同步清洗：取 jobList、丢掉缺主键/岗位名/公司名的、同页按
    `encrypt_job_id` 去重，并记下丢弃原因。

    Args:
        payload: 列表接口响应。
        page: 页码。

    Returns:
        一页的清洗结果。
    """
    data = payload.get("zpData")
    if data is None:
        data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    items = data.get("jobList")
    if not isinstance(items, list):
        items = []

    kept: list[Job] = []
    dropped: list[str] = []
    seen: set[str] = set()

    for index, item in enumerate(items):
        try:
            job = Job.from_api(item, page=page)
        except (TypeError, ValueError) as exc:
            dropped.append(f"第 {index + 1} 条解析失败：{exc}")
            continue
        if not job.is_valid:
            dropped.append(
                f"第 {index + 1} 条字段不全（jobName={job.job_name!r}, "
                f"brand={job.brand_name!r}, id={job.encrypt_job_id!r}）"
            )
            continue
        if job.encrypt_job_id in seen:
            dropped.append(f"第 {index + 1} 条同页重复：{job.encrypt_job_id}")
            continue
        seen.add(job.encrypt_job_id)
        kept.append(job)

    return PageResult(
        page=page,
        jobs=tuple(kept),
        has_more=bool(data.get("hasMore")),
        raw_count=len(items),
        dropped=tuple(dropped),
        lid=clean_text(data.get("lid")),
    )


def clean_pages(pages: Iterable[dict[str, Any]]) -> tuple[Job, ...]:
    """把多页响应洗成一个职位元组（测试/离线回放用）。"""
    out: list[Job] = []
    for index, payload in enumerate(pages, start=1):
        out.extend(clean_page(payload, page=index).jobs)
    return tuple(out)


@dataclass(frozen=True)
class ExpectItem:
    """一条求职期望。

    `encrypt_expect_id` 是统一出口；底层键名不对称，解析时两个键名都认。

    Attributes:
        kind: mix / expect / part_time。
        encrypt_expect_id: 加密期望 id；mix/part_time 用 encryptExpectId，expect 用 encryptId。
        position_name: 期望职位名。
        location_name: 显示名（如「佛山」）。
        tag_name: 标签名。
        mix_expect_type: 混合期望类型（仅 mix 有）。
        location_code: 混合期望的 city code（仅 mix 用得上）。
    """

    kind: str
    encrypt_expect_id: str
    position_name: str
    location_name: str = ""
    tag_name: str = ""
    mix_expect_type: str = ""
    location_code: str = ""

    @property
    def label(self) -> str:
        """人读名：「前端开发工程师(佛山)」这类组合，缺哪段跳过哪段。"""
        if self.position_name and self.location_name:
            return f"{self.position_name}({self.location_name})"
        return self.position_name or self.location_name or self.encrypt_expect_id


@dataclass(frozen=True)
class ExpectList:
    """账号的期望集合：混合期望（推荐 tab）+ 我的期望 + 兼职期望。

    Attributes:
        mix: 混合期望。
        expects: 我的期望列表。
        part_time: 兼职期望列表。
    """

    mix: ExpectItem | None = None
    expects: tuple[ExpectItem, ...] = ()
    part_time: tuple[ExpectItem, ...] = ()

    def pick(self, kind: str) -> ExpectItem | None:
        """按前端默认规则取一条：mix / expect / part_time 各取各的首条。"""
        if kind == "recommend":
            return self.mix
        if kind == "expect":
            return self.expects[0] if self.expects else None
        if kind == "part_time":
            return self.part_time[0] if self.part_time else None
        return None


def _expect_item(raw: Any, kind: str) -> ExpectItem | None:
    """把一条原始期望对象洗成 `ExpectItem`；认不出就丢。

    字段名不对称（encryptId / encryptExpectId）两个键名都认。
    """
    if not isinstance(raw, dict):
        return None
    encrypt_id = clean_text(raw.get("encryptExpectId") or raw.get("encryptId"))
    position = clean_text(raw.get("positionName") or raw.get("position"))
    location_name = clean_text(raw.get("locationName"))
    location_code = clean_text(raw.get("location"))
    tag = clean_text(raw.get("tagName"))
    mix_type = clean_text(raw.get("mixExpectType"))
    if not encrypt_id:
        return None
    return ExpectItem(
        kind=kind,
        encrypt_expect_id=encrypt_id,
        position_name=position,
        location_name=location_name,
        tag_name=tag,
        mix_expect_type=mix_type,
        location_code=location_code,
    )


def clean_expects(payload: dict) -> ExpectList:
    """把期望列表响应洗成 `ExpectList`。

    缺字段、类型不对一律按空处理，不抛——账号没配兼职期望是常态。
    """
    data = payload.get("zpData")
    if data is None:
        data = payload.get("data")
    if not isinstance(data, dict):
        return ExpectList()

    mix_raw = data.get("mixExpect")
    mix = _expect_item(mix_raw, "mix") if isinstance(mix_raw, dict) else None

    def _collect(key: str, kind: str) -> tuple[ExpectItem, ...]:
        """收集列表字段为期望元组。"""
        items = data.get(key)
        if not isinstance(items, list):
            return ()
        out = [item for item in (_expect_item(raw, kind) for raw in items) if item is not None]
        return tuple(out)

    return ExpectList(
        mix=mix,
        expects=_collect("expectList", "expect"),
        part_time=_collect("partTimeExpectList", "part_time"),
    )


def extract_job_desc(payload: dict[str, Any]) -> str:
    """从详情响应里抠出 JD 正文并清洗。

    优先 `zpData.jobInfo.postDescription`，再兜几个同义位置。
    """
    data = payload.get("zpData")
    if data is None:
        data = payload.get("data")
    if not isinstance(data, dict):
        return ""

    job_info = data.get("jobInfo")
    if isinstance(job_info, dict):
        for key in ("postDescription", "postDescriptionPlain", "jobDesc", "description"):
            if job_info.get(key):
                return clean_desc(job_info.get(key))

    for key in ("postDescription", "postDescriptionPlain", "jobDesc", "description"):
        if data.get(key):
            return clean_desc(data.get(key))
    return ""


def _as_str(value: Any) -> str:
    """任意值转干净字符串；`None` → 空串。"""
    if value is None:
        return ""
    return clean_text(value) if isinstance(value, str) else str(value)


def _as_int(value: Any, *, default: int = 0) -> int:
    """任意值转 int；`None` 或转不动就回默认。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
