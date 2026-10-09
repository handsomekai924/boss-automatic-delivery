"""筛选条件的数据传输对象。

对外只暴露 dataclass，屏蔽服务端返回结构（``zpData`` 里一堆 ``XxxList``）的差异。
code 一律规范成 ``str``：接口里是 int，URL 查询串里又是字符串，统一后比较省心。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator


def normalize_code(value: Any) -> str:
    """把接口里的 int / str code 统一成 str。``None`` → 空串。

    ``bool`` 虽是 int 子类，但当 code 没意义，原样字符串化。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@dataclass(frozen=True)
class FilterOption:
    """一个可选筛选值，如「1-3年」(code=104)。"""

    code: str
    name: str
    #: 薪资档专用：下限/上限，单位 K（千元）。非薪资档为 ``None``。
    low_salary: int | None = None
    high_salary: int | None = None

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "FilterOption":
        """从接口的 ``{code, name, lowSalary?, highSalary?}`` 构造。"""
        if not isinstance(item, dict):
            raise TypeError(f"筛选项不是对象: {type(item).__name__}")
        low = item.get("lowSalary")
        high = item.get("highSalary")
        return cls(
            code=normalize_code(item.get("code")),
            name=str(item.get("name") or ""),
            low_salary=None if low is None else int(low),
            high_salary=None if high is None else int(high),
        )

    @classmethod
    def from_tuple(cls, row: tuple) -> "FilterOption":
        """从 :mod:`boss_filter.tables` 的常量行构造。

        行形状：``(code, name)`` 或薪资档的 ``(code, name, low, high)``。
        """
        code, name = row[0], row[1]
        if len(row) >= 4:
            return cls(code=str(code), name=str(name), low_salary=int(row[2]), high_salary=int(row[3]))
        return cls(code=str(code), name=str(name))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "name": self.name}
        if self.low_salary is not None:
            out["lowSalary"] = self.low_salary
        if self.high_salary is not None:
            out["highSalary"] = self.high_salary
        return out


@dataclass(frozen=True)
class IndustryGroup:
    """公司行业的一组，如「互联网/AI」下的互联网、电子商务……"""

    name: str
    options: tuple[FilterOption, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "options": [o.to_dict() for o in self.options]}


@dataclass(frozen=True)
class CityNode:
    """城市树的一个节点：省 → 市 → 区。

    线上 ``/wapi/zpCommon/data/city.json`` 用 ``subLevelModelList`` 递归嵌套，
    这里拍平成 ``children``。兜底表只有热点城市一层，``children`` 为空。
    """

    code: str
    name: str
    pinyin: str = ""
    first_char: str = ""
    children: tuple["CityNode", ...] = ()

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "CityNode":
        raw_children = item.get("subLevelModelList") or []
        children = tuple(cls.from_api(c) for c in raw_children if isinstance(c, dict))
        return cls(
            code=normalize_code(item.get("code")),
            name=str(item.get("name") or ""),
            pinyin=str(item.get("pinyin") or ""),
            first_char=str(item.get("firstChar") or ""),
            children=children,
        )

    def walk(self) -> Iterator["CityNode"]:
        """先自己，再深度优先遍历子孙。"""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "name": self.name}
        if self.pinyin:
            out["pinyin"] = self.pinyin
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        return out


@dataclass(frozen=True)
class FilterConditions:
    """7 类筛选条件，外加接口顺带返回的辅助项。

    字段顺序对应推荐流程里用户会配置的维度；``source`` 标明数据从哪来，
    便于 UI 提示「当前是离线兜底值，可能不是最新的」。
    """

    # ---- 主维度（城市 / 求职类型 / 薪资 / 经验 / 学历 / 行业 / 规模） ----
    cities: tuple[CityNode, ...] = ()
    job_types: tuple[FilterOption, ...] = ()
    salaries: tuple[FilterOption, ...] = ()
    experiences: tuple[FilterOption, ...] = ()
    degrees: tuple[FilterOption, ...] = ()
    industries: tuple[IndustryGroup, ...] = ()
    scales: tuple[FilterOption, ...] = ()

    # ---- 接口顺带返回的辅助项 ----
    pay_types: tuple[FilterOption, ...] = ()
    stages: tuple[FilterOption, ...] = ()
    part_times: tuple[FilterOption, ...] = ()
    hot_cities: tuple[FilterOption, ...] = ()

    #: "api" = 线上接口；"fallback" = 离线兜底表；"html" = 解析已保存页面
    source: str = "api"
    #: 本次装配过程中被降级的来源，如 ``{"cities": "fallback"}``
    degraded: dict[str, str] = field(default_factory=dict)


    def industry_options(self) -> tuple[FilterOption, ...]:
        """把分组的行业摊平成一维列表（组名丢弃，code 全局唯一）。"""
        return tuple(opt for group in self.industries for opt in group.options)

    def find_option(self, kind: str, code: str) -> FilterOption | None:
        """按 code 查某个维度的选项。``kind`` 取字段名，如 ``"salaries"``。"""
        target = getattr(self, kind, None)
        if not isinstance(target, tuple):
            raise KeyError(f"没有名为 {kind!r} 的筛选维度")
        code = normalize_code(code)
        for opt in target:
            if isinstance(opt, FilterOption) and opt.code == code:
                return opt
        return None

    def find_city(self, code: str) -> CityNode | None:
        code = normalize_code(code)
        for node in self.cities:
            hit = _find_in_tree(node, code)
            if hit is not None:
                return hit
        return None

    def find_city_by_name(self, name: str) -> CityNode | None:
        for node in self.cities:
            for candidate in node.walk():
                if candidate.name == name:
                    return candidate
        return None

    def to_dict(self) -> dict[str, Any]:
        """导出为 JSON 友好的 dict（CLI ``export`` 用）。"""
        return {
            "source": self.source,
            "degraded": dict(self.degraded),
            "city": [c.to_dict() for c in self.cities],
            "hotCity": [c.to_dict() for c in self.hot_cities],
            "jobType": [c.to_dict() for c in self.job_types],
            "salary": [c.to_dict() for c in self.salaries],
            "experience": [c.to_dict() for c in self.experiences],
            "degree": [c.to_dict() for c in self.degrees],
            "industry": [g.to_dict() for g in self.industries],
            "scale": [c.to_dict() for c in self.scales],
            "payType": [c.to_dict() for c in self.pay_types],
            "stage": [c.to_dict() for c in self.stages],
            "partTime": [c.to_dict() for c in self.part_times],
        }

    def summary_lines(self) -> list[str]:
        """人读的摘要（CLI ``show`` 用），每维一行。"""
        def _row(label: str, items: Iterable) -> str:
            items = list(items)
            return f"{label:<8} {len(items):>3} 项"

        lines = [
            f"来源: {self.source}"
            + (f"（降级: {', '.join(f'{k}={v}' for k, v in self.degraded.items())}）" if self.degraded else ""),
            _row("城市", self.cities),
            _row("求职类型", self.job_types),
            _row("薪资待遇", self.salaries),
            _row("工作经验", self.experiences),
            _row("学历要求", self.degrees),
            _row("公司行业", self.industry_options()),
            _row("公司规模", self.scales),
        ]
        if self.hot_cities:
            lines.append(_row("热点城市", self.hot_cities))
        for label, items in (
            ("结算方式", self.pay_types),
            ("融资阶段", self.stages),
            ("兼职类型", self.part_times),
        ):
            if items:
                lines.append(_row(label, items))
        return lines


def _find_in_tree(node: CityNode, code: str) -> CityNode | None:
    if node.code == code:
        return node
    for child in node.children:
        hit = _find_in_tree(child, code)
        if hit is not None:
            return hit
    return None


def options_from_api(items: Any) -> tuple[FilterOption, ...]:
    """把接口的 list[dict] 拍成 ``FilterOption`` 元组；非 list 按空处理。"""
    if not isinstance(items, list):
        return ()
    return tuple(FilterOption.from_api(it) for it in items if isinstance(it, dict))


def options_from_rows(rows: Iterable[tuple]) -> tuple[FilterOption, ...]:
    """把 :mod:`boss_filter.tables` 的常量行拍成 ``FilterOption`` 元组。"""
    return tuple(FilterOption.from_tuple(row) for row in rows)
