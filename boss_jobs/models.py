"""职位的数据传输对象与**即时清洗**规则。

对外只暴露 dataclass，屏蔽服务端返回结构的差异。清洗发生在
:meth:`Job.from_api` / :func:`clean_page` 里——**每拿到一页就洗一页**，
洗完的干净职位才入库，原始 item 原样挂在 ``raw_json`` 上便于追溯。

todo.md 第四节要留的 8 个字段，与接口字段的对应：

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
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import config as C


# --------------------------------------------------------------------------- #
# 文本清洗小工具
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")


def clean_text(value: Any) -> str:
    """去掉首尾空白、把连续空白压成一个空格。``None`` → 空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return _WS_RE.sub(" ", value).strip()


def decode_salary_font(value: Any) -> str:
    """把防爬字体的私有区数字还原成 0-9。

    映射来自 chunk 的 ``S=["&#xe031;",…,"&#xe03a;"]``（下标即数字）。
    JSON 接口回的 ``salaryDesc`` 已经是明文，这里只处理 HTML 兜底解析
    和历史脏数据里可能出现的两种写法：``&#xe031;`` 与 ``\\ue031``。
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


# --------------------------------------------------------------------------- #
# 职位
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Job:
    """一条清洗后的职位。字段顺序对应 todo.md 第四节的 8 项。"""

    # ---- todo.md 列出的 8 项 ----
    job_name: str                 # 岗位名称
    brand_name: str               # 公司名称
    location: str                 # 工作地点（三段拼好的）
    salary_desc: str              # 薪资待遇
    job_experience: str           # 工作经验要求
    job_degree: str               # 学历要求
    brand_industry: str           # 公司行业
    brand_scale_name: str         # 公司规模

    # ---- 主键与追溯 ----
    encrypt_job_id: str           # 职位加密 id，全站唯一，入库主键
    raw_json: str = ""            # 原始 item，清洗前后可对照

    # ---- 地点分量（拼好之外另存，方便按区筛）----
    city_name: str = ""
    area_district: str = ""
    business_district: str = ""

    # ---- 接口顺带回的，todo.md 没列但先收下 ----
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

    #: 这条是从第几页捞的（入库后才有意义）
    page: int = 0

    @classmethod
    def from_api(cls, item: dict[str, Any], *, page: int = 0) -> "Job":
        """从接口的 jobList 元素构造。**不负责校验**，校验在 :func:`clean_page`。"""
        if not isinstance(item, dict):
            raise TypeError(f"职位不是对象: {type(item).__name__}")

        city = clean_text(item.get("cityName"))
        district = clean_text(item.get("areaDistrict"))
        business = clean_text(item.get("businessDistrict"))

        experience = clean_text(item.get("jobExperience"))
        degree = clean_text(item.get("jobDegree"))
        labels = as_str_list(item.get("jobLabels"))
        # jobExperience/jobDegree 偶发为空，退而从 jobLabels 的前两位认
        # （推荐页的 tag-list 顺序就是「经验 · 学历 · 技能…」）。
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
    """一页的清洗结果。``dropped`` 是被丢掉的原始条数与原因。"""

    page: int
    jobs: tuple[Job, ...] = ()
    has_more: bool = False
    raw_count: int = 0
    dropped: tuple[str, ...] = ()
    lid: str = ""

    @property
    def dropped_count(self) -> int:
        return self.raw_count - len(self.jobs)

    @property
    def is_empty(self) -> bool:
        return not self.jobs


# --------------------------------------------------------------------------- #
# 页级清洗
# --------------------------------------------------------------------------- #


def clean_page(payload: dict[str, Any], *, page: int) -> PageResult:
    """把一页响应洗成 :class:`PageResult`。

    做的事（**同步、立刻**，不缓存原始响应）：

    1. 取 ``zpData.jobList``；
    2. 逐条 :meth:`Job.from_api`，丢掉缺主键/岗位名/公司名的；
    3. 同页内按 ``encrypt_job_id`` 去重（接口偶尔会在相邻页重推）；
    4. 记下丢弃原因，供 CLI 报告「本页 15 条，洗后 13 条」。
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


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    return clean_text(value) if isinstance(value, str) else str(value)


def _as_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
