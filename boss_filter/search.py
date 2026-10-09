"""搜索条件装配：把筛选维度拼成 ``/wapi/zpgeek/search/joblist.json`` 的查询串。

参数名对齐职位页 ``getFormData()``：``city/experience/payType/partTime/degree/
industry/scale/salary/jobType`` + ``query/page/pageSize/scene/encryptExpectId``。
多选维度（``payType/partTime/experience/degree/industry/scale/stage``）逗号 join，
单选维度（``city/jobType/salary``）原样传。

选中值一律用 :class:`~boss_filter.models.FilterConditions` 里的 **code**
（如薪资 10-20K = ``405``），不是显示名。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import boss_db

from .models import FilterConditions, normalize_code


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

    @property
    def is_blank(self) -> bool:
        """True = 一个筛选维度都没选（关键词也没有），等于「不限」。"""
        return not any(
            (
                self.query.strip(),
                self.city,
                self.job_type,
                self.salary,
                self.experience,
                self.degree,
                self.industry,
                self.scale,
                self.pay_type,
                self.part_time,
                self.stage,
            )
        )


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



#: 配置文件里认的键 → :class:`JobSearchFilter` 的字段。
#: 单选维度收字符串，多选维度收数组（也兼容逗号串）。``page``/``scene``
#: 是运行期的，不进配置。
FILTER_FILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("query", "query"),
    ("city", "city"),
    ("job_type", "job_type"),
    ("jobType", "job_type"),
    ("salary", "salary"),
    ("experience", "experience"),
    ("degree", "degree"),
    ("industry", "industry"),
    ("scale", "scale"),
    ("pay_type", "pay_type"),
    ("payType", "pay_type"),
    ("part_time", "part_time"),
    ("partTime", "part_time"),
    ("stage", "stage"),
    ("page_size", "page_size"),
    ("pageSize", "page_size"),
)

#: 单选字段（其余按多选收）
_SINGLE_FIELDS: frozenset[str] = frozenset({"query", "city", "job_type", "salary"})

#: 写模板时用的字段顺序（camelCase，跟站点查询串对齐）
TEMPLATE_KEYS: tuple[str, ...] = (
    "query",
    "city",
    "jobType",
    "salary",
    "experience",
    "degree",
    "industry",
    "scale",
    "payType",
    "partTime",
    "stage",
    "pageSize",
)


def filter_path(path: Path | str | None = None) -> Path:
    """状态库路径：显式参数 → ``BOSS_DB`` → ``data/boss.db``。

    搜索条件是库里的一行（``doc('search_filter')``），不是单独的配置文件了。
    用户点名的 JSON 文件走 :func:`search_filter_from_file`。
    """
    return boss_db.resolve_db_path(path)


def _to_codes(value: Any) -> tuple[str, ...]:
    """多选值收数组或逗号串，统一成 code 元组。"""
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = (value,)
    return tuple(code for code in (normalize_code(v).strip() for v in raw) if code)


def search_filter_from_dict(data: Mapping[str, Any]) -> JobSearchFilter:
    """把配置文件的内容装配成 :class:`JobSearchFilter`。

    认 snake_case 和 camelCase 两种键名；缺的键、空值一律留空
    （= 该维度「不限」）。``page``/``scene`` 不收——翻页是运行期的事。
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"筛选条件配置得是 JSON 对象，收到 {type(data).__name__}")

    fields: dict[str, Any] = {}
    for key, attr in FILTER_FILE_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if attr in _SINGLE_FIELDS:
            fields[attr] = normalize_code(value).strip() if attr != "query" else str(value or "").strip()
        elif attr == "page_size":
            try:
                fields[attr] = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"pageSize 得是正整数，收到 {value!r}") from None
        else:
            fields[attr] = _to_codes(value)

    # 同一字段给了两种键名（job_type 与 jobType）时，后写的胜出
    return JobSearchFilter(**fields)


def search_filter_from_file(path: Path | str) -> JobSearchFilter:
    """只读用户点名的 JSON 文件（CLI ``--filter my.json`` 用），**不写库**。

    这是「本次抓取就用这份条件」的运行期入参，不是持久状态，所以仍走文件。
    格式同 :func:`search_filter_from_dict`。

    :raises ValueError: 文件读不了、不是 JSON、或不是 JSON 对象
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"筛选条件配置 {p} 不存在") from None
    except OSError as exc:
        raise ValueError(f"读不了筛选条件配置 {p}：{exc}") from exc

    if not text.strip():
        return JobSearchFilter()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"筛选条件配置 {p} 不是合法 JSON：{exc}") from exc
    return search_filter_from_dict(data)


def load_search_filter(path: Path | str | None = None) -> JobSearchFilter:
    """从状态库读筛选条件。**库里没有就留空**（= 全部「不限」），不报错。

    库里存的是一个 JSON 对象，键跟站点查询串对齐，值留空 = 该维度「不限」::

        {
          "query": "python",
          "city": "101280100",
          "jobType": "",
          "salary": "405",
          "experience": ["104", "105"],
          "degree": ["209"],
          "industry": [],
          "scale": [],
          "pageSize": 15
        }

    code 取值见 :class:`~boss_filter.models.FilterConditions`，
    ``python -m boss_filter export`` 能把整张表导出来对照。

    :param path: 状态库路径；不传按 :func:`filter_path` 定位
    :return: 库里有就按那行装配（可全空）；没有就 :class:`JobSearchFilter()` 全空
    :raises ValueError: payload 不是 JSON 对象、或 pageSize 形状不对
    """
    try:
        raw = boss_db.doc_get_raw(boss_db.DOC_SEARCH_FILTER, path)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"读不了状态库 {filter_path(path)}：{exc}") from exc

    if raw is None or not raw.strip():
        return JobSearchFilter()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"状态库里的筛选条件不是合法 JSON：{exc}") from exc
    return search_filter_from_dict(data)


def save_search_filter(
    search_filter: JobSearchFilter,
    path: Path | str | None = None,
) -> Path:
    """把筛选条件写回状态库（camelCase，空值留空串/空数组）。返回库路径。"""
    data = {
        "query": search_filter.query,
        "city": search_filter.city,
        "jobType": search_filter.job_type,
        "salary": search_filter.salary,
        "experience": list(search_filter.experience),
        "degree": list(search_filter.degree),
        "industry": list(search_filter.industry),
        "scale": list(search_filter.scale),
        "payType": list(search_filter.pay_type),
        "partTime": list(search_filter.part_time),
        "stage": list(search_filter.stage),
        "pageSize": search_filter.page_size,
    }
    return boss_db.doc_set(boss_db.DOC_SEARCH_FILTER, data, path)
