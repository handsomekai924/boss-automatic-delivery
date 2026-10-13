"""期望流（``recommend/job/list.json``）的参数装配。

「求职期望流」是第三条检索路径：推荐 / 兼职 / 指定期望三个 tab 共用同一条
接口，差异只在参数形态（站点 chunk ``job~0.0c757050.js`` 的 ``getFormData()``
是**四**分支，含临时期望）。这里刻意**不塞进** :class:`~boss_filter.search.
JobSearchFilter`——参数形状是三选一的模式，混进 ``to_params()`` 会让那条搜索流
的分支难测。独立小类更贴前端 ``getFormData()``。

kind 对应前端 ``currentJobTab``：

========  ==========================================================
kind      期望主参数
========  ==========================================================
recommend ``city`` + ``encryptExpectId`` + ``mixExpectType``（推荐 tab）
part_time ``jobType=1903`` + ``encryptExpectId``（兼职 tab）
expect    ``encryptExpectId``（指定期望 tab）
temp      ``expectInfo``（JSON，临时期望 tab）
========  ==========================================================

筛选维度（``city/salary/experience/degree/industry/scale``，兼职态
``payType/partTime``）与搜索流同一套 code、同一套拼法（多选逗号 join）——
期望流**可以带筛选，就是不带关键词**。故意不收 ``query``/``position``/``stage``
等 Y 类键：前端点期望 tab 时 ``onExpectChange`` 会 ``delete formData[Y]``
（``Y`` 首位就是 ``query``），期望流请求里永远没有关键词。要关键词就走搜索流
（``JobSearchFilter.encrypt_expect_id``，「期望 + 关键词」组合），不要往期望流里
塞 query——那不是站点的行为。

另外注意：**改筛选不切搜索态**（前端 ``onFilterChange`` 不动 ``isSearch``），
所以「期望 + 薪资/经验/城市」是在期望流上表达的，不是搜索流。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

# 与搜索流同一套拼法——**不复制一份**，两套拼法将来改一处漏一处。
from .search import JobSearchFilter, _join, _one

#: 四种期望形态（前端 ``getFormData()`` 的分支）。``temp_expect`` 站点 UI 上是
#: 「快速添加」的临时检索，采集场景用得少，UI 下拉可后置，但参数形态先支持。
EXPECT_KINDS: tuple[str, ...] = ("recommend", "part_time", "expect", "temp_expect")

#: 兼职态写死的求职类型 code（前端 ``getFormData()`` 的 ``jobType=1903``）。
PART_TIME_JOB_TYPE: str = "1903"

#: 永不进查询串的 Y 类键。期望态前端 ``getRemovedFields()`` 剔的就是这批，
#: 其中 ``query``/``scene`` 最容易被顺手带上，单独记一笔。
NEVER_PARAMS: frozenset[str] = frozenset(
    {"query", "position", "stage", "multiBusinessDistrict", "multiSubway", "areaBusiness", "scene"}
)


def _expect_info_json(info: Mapping[str, Any]) -> str:
    """把临时期望对象序列化成 ``expectInfo`` 串。

    形状对齐前端 ``getFormData()`` 的 temp-expect 分支::

        JSON.stringify({expectType, position, location, lowSalary, highSalary})

    注意键名是 ``position``/``location``，**不是** ``positionName``/``locationName``。
    """
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
    """期望流（``recommend/job/list.json``）的一次检索。

    筛选维度与 :class:`~boss_filter.search.JobSearchFilter` 同一套 code、同一套
    拼法；差别是主参数从「关键词」换成「期望 id」，且**永远没有 query**。
    """

    kind: str                          # "recommend" | "part_time" | "expect" | "temp_expect"
    encrypt_expect_id: str = ""
    mix_expect_type: str = ""
    expect_info: str = ""              # 仅 temp_expect：JSON 串，形状见 _expect_info_json
    # ---- 共享筛选维度（空 = 不限，不进查询串）----
    city: str = ""
    salary: str = ""                   # 单选；part_time 态忽略（被 payType/partTime 顶替）
    experience: tuple[str, ...] = ()   # 多选
    degree: tuple[str, ...] = ()
    industry: tuple[str, ...] = ()
    scale: tuple[str, ...] = ()
    pay_type: tuple[str, ...] = ()     # 仅 part_time 态生效
    part_time: tuple[str, ...] = ()    # 仅 part_time 态生效
    page: int = 1
    page_size: int = 15

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError(f"页码从 1 开始，收到 {self.page}")
        if self.page_size < 1:
            raise ValueError(f"每页条数至少 1，收到 {self.page_size}")

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #

    def to_params(self) -> dict[str, str]:
        """拼成期望流的查询串参数。**空值不进参数**，让站点走自己的默认。

        ① 期望主参数按 kind 四选一；② 共享筛选维度四种 kind 都可传，
        但兼职态 ``salary`` 被 ``payType``/``partTime`` 顶替。
        ``query``/``scene`` 等 Y 类键**永远不出现**（见模块头）。

        这套参数是给 **GET** 用的——**实测（2026-10-13）**同参数 POST 回
        code 10「未知请求方法」，前端 ``hc`` 封装那条 POST form 不能照搬。
        另外**不要**给搜索流补 ``partTimeExpect``：兼职态只发
        ``encryptExpectId`` 就够（见下面的分支注释）。
        """
        params: dict[str, str] = {
            "page": str(self.page),
            "pageSize": str(self.page_size),
        }

        # ① 期望主参数（按 kind 四选一）
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
            # 前端 getConfigFields() 兼职态还会把 partTimeExpect 一并透传，
            # 但 getFormData() 主参数是同值的 encryptExpectId。**实测（2026-10-13，
            # 本账号有 4 条兼职期望后复测）**：只发 encryptExpectId + jobType=1903
            # 就 code 0 / 15 条 / hasMore=true；再叠一份 partTimeExpect 同样
            # code 0——**服务端不要它**，所以这里刻意只发 encryptExpectId。
        elif self.kind == "expect":
            if self.encrypt_expect_id:
                params["encryptExpectId"] = self.encrypt_expect_id
        elif self.kind == "temp_expect":
            # 前端：expectInfo = JSON.stringify({expectType, position, location,
            #                                    lowSalary, highSalary})
            if self.expect_info:
                params["expectInfo"] = self.expect_info
        else:
            raise ValueError(f"未知期望类型：{self.kind}")

        # ② 共享筛选维度（四种 kind 都可传；空值不进参数）
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

        # 防御：万一调用方往这个 dataclass 上挂了 Y 类键（或上游 dict 塞进来），
        # 这里兜一层，保证查询串永远干净。
        for key in NEVER_PARAMS:
            params.pop(key, None)
        return params

    def for_page(self, page: int) -> "JobExpectFilter":
        """同一套条件换个页码（翻页用）。"""
        return replace(self, page=page)

    # ------------------------------------------------------------------ #
    # 从期望列表 / 临时期望 / 搜索条件构造
    # ------------------------------------------------------------------ #

    @classmethod
    def from_expects(
        cls,
        expects: Any,
        kind: str,
        *,
        city: str = "",
        **filters: Any,
    ) -> "JobExpectFilter":
        """按前端默认规则从期望列表取一条：mix / ``partTimeExpectList[0]`` /
        ``expectList[0]``。

        「默认取哪个」的规则集中在这一处，不散到调用方。推荐 tab 还要带出
        ``mixExpectType``，并且 ``city`` 默认取混合期望的 ``location``（前端
        ``getFormData().city = mixLocation``）。

        :param expects: :class:`~boss_jobs.models.ExpectList`
        :param kind: ``recommend`` / ``part_time`` / ``expect``
        :param city: 覆盖城市 code；不传则推荐 tab 用混合期望的 location
        :raises ValueError: 该形态下没有可取的期望
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
        """临时期望：把 ``{expectType, position, location, lowSalary, highSalary}``
        序列化成 ``expectInfo``。

        ``info`` 也可以直接给已经序列化好的串（``str``），那样原样透传。
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
        """把搜索条件里的**共享筛选维度**搬过来（采集配置复用同一份 search payload）。

        只搬筛选，**不搬** ``query``/``scene``/``stage``/``job_type``——期望流永远
        不带关键词，``stage`` 是 Y 类键，``jobType`` 只在兼职态由本类写死 1903。
        """
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
