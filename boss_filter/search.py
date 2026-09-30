"""搜索条件装配：把 todo.md 第二节的筛选维度拼成搜索接口的查询串。

「JobSearchFilter」就是这一步——用户选好城市/求职类型/薪资/经验/学历/行业/规模
之后，把这些选中值编成 ``/wapi/zpgeek/search/joblist.json`` 认的参数。

参数名不是猜的，来自职位页 chunk ``job~1.59fdd3bf.js`` 的 ``getFormData()``::

    {city, experience, payType, partTime, degree, industry, scale,
     salary, jobType}          ← 数组用逗号 join，单项原样
    + query / page / pageSize / scene / encryptExpectId

对照同 chunk 的筛选状态 ``Q``：``payType/partTime/experience/degree/
industry/scale/stage`` 是多选，``city/jobType/salary`` 是单选。

选中值一律用 :class:`~boss_filter.models.FilterConditions` 里的 **code**
（如薪资 10-20K = ``405``），不是显示名。code 从哪来见 :mod:`boss_filter`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import FilterConditions, FilterOption, normalize_code


def _join(values: Iterable[Any]) -> str:
    """多选值拍成逗号串；空/None 自动丢掉。"""
    out: list[str] = []
    for value in values:
        code = normalize_code(value)
        if code:
            out.append(code)
    return ",".join(out)


def _one(value: Any) -> str:
    """单选值；空/None → 空串（空串不进查询串）。"""
    return normalize_code(value)


@dataclass(frozen=True)
class JobSearchFilter:
    """一次职位搜索的条件。

    **code 而非显示名**：``city="101280100"``（广州），``salary="405"``（10-20K）。
    多选维度收元组，单选维度收字符串。什么都不填 = 站点默认（当前城市全量）。

    :param query: 搜索关键词，如「python」。空 = 不按关键词搜
    :param city: 城市 code，如广州 ``101280100``。空 = 站点当前城市
    :param job_type: 求职类型 code（单选），如全职 ``1901``
    :param salary: 薪资档 code（单选），如 ``405`` = 10-20K
    :param experience: 工作经验 code（多选），如 ``("104","105")``
    :param degree: 学历要求 code（多选）
    :param industry: 公司行业 code（多选）
    :param scale: 公司规模 code（多选）
    :param pay_type: 结算方式 code（多选，接口顺带回的辅助维度）
    :param part_time: 兼职类型 code（多选）
    :param stage: 融资阶段 code（多选）
    :param page: 页码，**从 1 开始**
    :param page_size: 每页条数（服务端默认 15）
    """

    query: str = ""
    city: str = ""
    job_type: str = ""
    salary: str = ""
    experience: tuple[str, ...] = ()
    degree: tuple[str, ...] = ()
    industry: tuple[str, ...] = ()
    scale: tuple[str, ...] = ()
    pay_type: tuple[str, ...] = ()
    part_time: tuple[str, ...] = ()
    stage: tuple[str, ...] = ()

    page: int = 1
    page_size: int = 15

    #: 搜索场景。职位页搜索态固定传 1（见 chunk ``onSearch``）
    scene: int = 1

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"页码从 1 开始，收到 {self.page}")
        if self.page_size < 1:
            raise ValueError(f"每页条数至少 1，收到 {self.page_size}")

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def to_params(self) -> dict[str, str]:
        """拼成搜索接口的查询串参数。**空值不进参数**，让站点走自己的默认。

        单选维度（``city/jobType/salary``）空了就不传；多选维度拼逗号串，
        全空同样不传。``query`` 为空时不传（区分「搜空串」和「不搜关键词」）。
        """
        params: dict[str, str] = {
            "page": str(self.page),
            "pageSize": str(self.page_size),
            "scene": str(self.scene),
        }

        if self.query.strip():
            params["query"] = self.query.strip()

        for key, value in (
            ("city", self.city),
            ("jobType", self.job_type),
            ("salary", self.salary),
        ):
            code = _one(value)
            if code:
                params[key] = code

        for key, values in (
            ("experience", self.experience),
            ("degree", self.degree),
            ("industry", self.industry),
            ("scale", self.scale),
            ("payType", self.pay_type),
            ("partTime", self.part_time),
            ("stage", self.stage),
        ):
            joined = _join(values)
            if joined:
                params[key] = joined

        return params

    def for_page(self, page: int) -> "JobSearchFilter":
        """同一套条件换个页码（翻页用）。"""
        return JobSearchFilter(
            query=self.query,
            city=self.city,
            job_type=self.job_type,
            salary=self.salary,
            experience=self.experience,
            degree=self.degree,
            industry=self.industry,
            scale=self.scale,
            pay_type=self.pay_type,
            part_time=self.part_time,
            stage=self.stage,
            page=page,
            page_size=self.page_size,
            scene=self.scene,
        )

    # ------------------------------------------------------------------ #
    # 从筛选条件构造
    # ------------------------------------------------------------------ #

    @classmethod
    def from_codes(
        cls,
        *,
        query: str = "",
        city: str = "",
        job_type: str = "",
        salary: str = "",
        experience: Iterable[str] = (),
        degree: Iterable[str] = (),
        industry: Iterable[str] = (),
        scale: Iterable[str] = (),
        pay_type: Iterable[str] = (),
        part_time: Iterable[str] = (),
        stage: Iterable[str] = (),
        page: int = 1,
        page_size: int = 15,
    ) -> "JobSearchFilter":
        """直接用 code 构造。code 的取值见 :class:`~boss_filter.models.FilterConditions`。"""
        return cls(
            query=query,
            city=city,
            job_type=job_type,
            salary=salary,
            experience=tuple(experience),
            degree=tuple(degree),
            industry=tuple(industry),
            scale=tuple(scale),
            pay_type=tuple(pay_type),
            part_time=tuple(part_time),
            stage=tuple(stage),
            page=page,
            page_size=page_size,
        )

    def resolve(self, conditions: FilterConditions) -> "ResolvedSearchFilter":
        """把 code 换回可读名字，便于展示「当前按什么条件在搜」。

        纯本地查表，不打接口。code 认不出来时名字留空串（不报错——
        筛选表可能比用户手上的 code 新/旧，不该因此挡掉一次搜索）。
        """
        def _names(kind: str, codes: Iterable[str]) -> tuple[str, ...]:
            out: list[str] = []
            for code in codes:
                hit = conditions.find_option(kind, code)
                out.append(hit.name if hit else "")
            return tuple(out)

        def _name(kind: str, code: str) -> str:
            if not code:
                return ""
            hit = conditions.find_option(kind, code)
            return hit.name if hit else ""

        def _industry_names(codes: Iterable[str]) -> tuple[str, ...]:
            """行业是分组表，先摊平再查（``find_option`` 认的是字段名，行业没有单字段）。"""
            flat = {opt.code: opt.name for opt in conditions.industry_options()}
            return tuple(flat.get(code, "") for code in codes)

        city_node = conditions.find_city(self.city) if self.city else None
        return ResolvedSearchFilter(
            query=self.query,
            city_code=self.city,
            city_name=city_node.name if city_node else "",
            job_type_code=self.job_type,
            job_type_name=_name("job_types", self.job_type),
            salary_code=self.salary,
            salary_name=_name("salaries", self.salary),
            experience_codes=self.experience,
            experience_names=_names("experiences", self.experience),
            degree_codes=self.degree,
            degree_names=_names("degrees", self.degree),
            industry_codes=tuple(self.industry),
            industry_names=_industry_names(self.industry),
            scale_codes=self.scale,
            scale_names=_names("scales", self.scale),
            page=self.page,
            page_size=self.page_size,
        )


@dataclass(frozen=True)
class ResolvedSearchFilter:
    """搜索条件的「人读版」：code 和名字并排，CLI/UI 展示用。"""

    query: str = ""
    city_code: str = ""
    city_name: str = ""
    job_type_code: str = ""
    job_type_name: str = ""
    salary_code: str = ""
    salary_name: str = ""
    experience_codes: tuple[str, ...] = ()
    experience_names: tuple[str, ...] = ()
    degree_codes: tuple[str, ...] = ()
    degree_names: tuple[str, ...] = ()
    industry_codes: tuple[str, ...] = ()
    industry_names: tuple[str, ...] = ()
    scale_codes: tuple[str, ...] = ()
    scale_names: tuple[str, ...] = ()
    page: int = 1
    page_size: int = 15

    def summary_lines(self) -> list[str]:
        def _row(label: str, names: tuple[str, ...], codes: tuple[str, ...]) -> str:
            pairs = [n or c for n, c in zip(names, codes)]
            return f"{label:<8} {'、'.join(pairs) if pairs else '不限'}"

        rows = [
            f"关键词   {self.query or '不限'}",
            f"城市     {self.city_name or self.city_code or '不限'}",
            f"求职类型 {self.job_type_name or self.job_type_code or '不限'}",
            f"薪资     {self.salary_name or self.salary_code or '不限'}",
            _row("工作经验", self.experience_names, self.experience_codes),
            _row("学历要求", self.degree_names, self.degree_codes),
            _row("公司行业", self.industry_names, self.industry_codes),
            _row("公司规模", self.scale_names, self.scale_codes),
            f"分页     第 {self.page} 页，每页 {self.page_size} 条",
        ]
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "city": {"code": self.city_code, "name": self.city_name},
            "jobType": {"code": self.job_type_code, "name": self.job_type_name},
            "salary": {"code": self.salary_code, "name": self.salary_name},
            "experience": [
                {"code": c, "name": n} for c, n in zip(self.experience_codes, self.experience_names)
            ],
            "degree": [
                {"code": c, "name": n} for c, n in zip(self.degree_codes, self.degree_names)
            ],
            "industry": [
                {"code": c, "name": n} for c, n in zip(self.industry_codes, self.industry_names)
            ],
            "scale": [
                {"code": c, "name": n} for c, n in zip(self.scale_codes, self.scale_names)
            ],
            "page": self.page,
            "pageSize": self.page_size,
        }
