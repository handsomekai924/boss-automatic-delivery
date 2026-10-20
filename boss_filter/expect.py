"""期望流（`recommend/job/list.json`）的参数装配。

推荐 / 兼职 / 指定期望 / 临时期望四个 tab 共用同一条接口，差异只在参数形态。
刻意不塞进 `JobSearchFilter`；期望流可以带筛选，但永远不带关键词。明细见
`notes/boss_filter/expect.md`。

Attributes:
    EXPECT_KINDS: 四种期望形态（`recommend` / `part_time` / `expect` / `temp_expect`）。
    PART_TIME_JOB_TYPE: 兼职态写死的求职类型 code。
    NEVER_PARAMS: 永不进查询串的 Y 类键。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .search import JobSearchFilter, _join, _one

EXPECT_KINDS: tuple[str, ...] = ("recommend", "part_time", "expect", "temp_expect")

PART_TIME_JOB_TYPE: str = "1903"

NEVER_PARAMS: frozenset[str] = frozenset(
    {"query", "position", "stage", "multiBusinessDistrict", "multiSubway", "areaBusiness", "scene"}
)


def _expect_info_json(info: Mapping[str, Any]) -> str:
    """把临时期望对象序列化成 `expectInfo` 串（键名是 `position`/`location`）。"""
    return json.dumps(
        {
            "expectType": info.get("expectType", ""),
            "position": info.get("position", ""),
            "location": info.get("location", ""),
            "lowSalary": info.get("lowSalary", ""),
            "highSalary": info.get("highSalary", ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class JobExpectFilter:
    """期望流的一次检索；筛选维度与 `JobSearchFilter` 同一套 code。

    Attributes:
        kind: 期望形态（`recommend` / `part_time` / `expect` / `temp_expect`）。
        encrypt_expect_id: 期望 id。
        mix_expect_type: 混合期望类型，仅推荐 tab。
        expect_info: 临时期望 JSON 串，仅 `temp_expect`。
        salary: 单选；兼职态忽略（被 `payType`/`partTime` 顶替）。
        pay_type: 结算方式，仅兼职态生效。
        part_time: 兼职类型，仅兼职态生效。
    """

    kind: str
    encrypt_expect_id: str = ""
    mix_expect_type: str = ""
    expect_info: str = ""
    city: str = ""
    salary: str = ""
    experience: tuple[str, ...] = ()
    degree: tuple[str, ...] = ()
    industry: tuple[str, ...] = ()
    scale: tuple[str, ...] = ()
    pay_type: tuple[str, ...] = ()
    part_time: tuple[str, ...] = ()
    page: int = 1
    page_size: int = 15

    def __post_init__(self) -> None:
        """校验页码与每页条数。"""
        if self.page < 1:
            raise ValueError(f"页码从 1 开始，收到 {self.page}")
        if self.page_size < 1:
            raise ValueError(f"每页条数至少 1，收到 {self.page_size}")

    def to_params(self) -> dict[str, str]:
        """拼成期望流的查询串参数；空值不进参数，`NEVER_PARAMS` 永不出现。"""
        params: dict[str, str] = {
            "page": str(self.page),
            "pageSize": str(self.page_size),
        }

        if self.kind == "recommend":
            if self.city:
                params["city"] = self.city
            if self.encrypt_expect_id:
                params["encryptExpectId"] = self.encrypt_expect_id
            if self.mix_expect_type:
                params["mixExpectType"] = self.mix_expect_type
        elif self.kind == "part_time":
            params["jobType"] = PART_TIME_JOB_TYPE
            if self.encrypt_expect_id:
                params["encryptExpectId"] = self.encrypt_expect_id
        elif self.kind == "expect":
            if self.encrypt_expect_id:
                params["encryptExpectId"] = self.encrypt_expect_id
        elif self.kind == "temp_expect":
            if self.expect_info:
                params["expectInfo"] = self.expect_info
        else:
            raise ValueError(f"未知期望类型：{self.kind}")

        if self.city and "city" not in params:
            params["city"] = self.city
        if self.kind != "part_time":
            code = _one(self.salary)
            if code:
                params["salary"] = code
        else:
            for key, values in (("payType", self.pay_type), ("partTime", self.part_time)):
                joined = _join(values)
                if joined:
                    params[key] = joined
        for key, values in (
            ("experience", self.experience),
            ("degree", self.degree),
            ("industry", self.industry),
            ("scale", self.scale),
        ):
            joined = _join(values)
            if joined:
                params[key] = joined

        for key in NEVER_PARAMS:
            params.pop(key, None)
        return params

    def for_page(self, page: int) -> "JobExpectFilter":
        """同一套条件换个页码（翻页用）。"""
        return replace(self, page=page)

    @classmethod
    def from_expects(
        cls,
        expects: Any,
        kind: str,
        *,
        city: str = "",
        **filters: Any,
    ) -> "JobExpectFilter":
        """按前端默认规则从期望列表取一条；没有可取期望或 kind 不认识时抛 `ValueError`。

        取 mix / `partTimeExpectList[0]` / `expectList[0]`；推荐 tab 带出 `mixExpectType`，
        `city` 默认取混合期望的 location。

        Args:
            expects: 期望列表（`boss_jobs.models.ExpectList`）。
            kind: `recommend` / `part_time` / `expect`。
            city: 覆盖城市 code；不传则推荐 tab 用混合期望的 location。
            **filters: 透传给本类的其余筛选字段。

        Returns:
            构造好的期望流条件。
        """
        if kind == "recommend":
            item = getattr(expects, "mix", None)
            label = "推荐（混合）期望"
        elif kind == "part_time":
            items = getattr(expects, "part_time", ()) or ()
            item = items[0] if items else None
            label = "兼职期望"
        elif kind == "expect":
            items = getattr(expects, "expects", ()) or ()
            item = items[0] if items else None
            label = "我的期望"
        elif kind == "temp_expect":
            raise ValueError("临时期望没有列表条目，请用 from_temp_expect() 直接给 expectInfo")
        else:
            raise ValueError(f"未知期望类型：{kind}")

        if item is None:
            raise ValueError(f"账号没有可取的{label}（先去站点配好，或显式给 encrypt_expect_id）")

        picked_city = city
        if kind == "recommend" and not picked_city:
            picked_city = getattr(item, "location_code", "") or ""
        return cls(
            kind=kind,
            encrypt_expect_id=item.encrypt_expect_id,
            mix_expect_type=item.mix_expect_type if kind == "recommend" else "",
            city=picked_city,
            **filters,
        )

    @classmethod
    def from_temp_expect(cls, info: Mapping[str, Any], **filters: Any) -> "JobExpectFilter":
        """临时期望：把 `{expectType, position, location, lowSalary, highSalary}` 序列化成 `expectInfo`。

        `info` 也可以直接给已经序列化好的串（`str`），那样原样透传。
        """
        if isinstance(info, str):
            payload = info
        else:
            payload = _expect_info_json(info)
        return cls(kind="temp_expect", expect_info=payload, **filters)

    @classmethod
    def from_search(
        cls,
        search: JobSearchFilter,
        *,
        kind: str,
        encrypt_expect_id: str = "",
        mix_expect_type: str = "",
        expect_info: str = "",
    ) -> "JobExpectFilter":
        """把搜索条件里的共享筛选维度搬过来；不搬 `query`/`scene`/`stage`/`job_type`。"""
        return cls(
            kind=kind,
            encrypt_expect_id=encrypt_expect_id,
            mix_expect_type=mix_expect_type,
            expect_info=expect_info,
            city=search.city,
            salary=search.salary,
            experience=tuple(search.experience),
            degree=tuple(search.degree),
            industry=tuple(search.industry),
            scale=tuple(search.scale),
            pay_type=tuple(search.pay_type),
            part_time=tuple(search.part_time),
            page=search.page,
            page_size=search.page_size,
        )

    def summary_lines(self) -> list[str]:
        """一行人读摘要（CLI 展示用）。"""
        kind_label = {
            "recommend": "推荐（混合）期望",
            "part_time": "兼职期望",
            "expect": "我的期望",
            "temp_expect": "临时期望",
        }.get(self.kind, self.kind)
        rows = [
            f"期望形态 {kind_label}",
            f"期望 id   {self.encrypt_expect_id or self.expect_info or '不限'}",
        ]
        if self.mix_expect_type:
            rows.append(f"混合类型 {self.mix_expect_type}")
        if self.city:
            rows.append(f"城市     {self.city}")
        if self.kind == "part_time":
            rows.append(f"结算方式 {'、'.join(self.pay_type) or '不限'}")
            rows.append(f"兼职类型 {'、'.join(self.part_time) or '不限'}")
        elif self.salary:
            rows.append(f"薪资     {self.salary}")
        return rows
