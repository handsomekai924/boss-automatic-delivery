"""筛选条件的线上接口客户端。

调的是真实 wapi 路径（见 :mod:`boss_filter.config`），不是界面模拟。
登录态复用 ``boss_login`` 落盘的 Cookie：筛选接口本身不强制登录，
但带上登录态能拿到定位城市、也更接近浏览器真实流量。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from . import config as C
from .errors import FilterApiError, FilterDataError, FilterError, FilterTransportError
from .fallback import build_fallback_conditions, conditions_from_html
from .models import (
    CityNode,
    FilterConditions,
    FilterOption,
    IndustryGroup,
    options_from_api,
    options_from_rows,
)
from . import tables

logger = logging.getLogger(__name__)


class FilterClient:
    """筛选条件客户端。

    :param base_url: 站点地址
    :param endpoints: 覆盖 :data:`config.ENDPOINTS` 里的路径（探路/测试用）
    :param http: 带 ``request()`` 的会话对象，默认新建 ``requests.Session``
    :param timeout: 单次请求超时（秒）
    :param retries: 失败重试次数（网络层 / 5xx）
    :param backoff: 重试退避基数（秒），按 2 的幂递增
    :param sleeper: 可替换的 sleep（测试里注入 no-op）
    """

    def __init__(
        self,
        *,
        base_url: str = C.BASE_URL,
        endpoints: Mapping[str, str] | None = None,
        http: Any | None = None,
        timeout: float = C.DEFAULT_TIMEOUT,
        retries: int = C.DEFAULT_RETRIES,
        backoff: float = 0.8,
        headers: Mapping[str, str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoints = {**C.ENDPOINTS, **(endpoints or {})}
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self._sleep = sleeper

        self._http = http or requests.Session()
        merged = {**C.DEFAULT_HEADERS, **(headers or {})}
        if hasattr(self._http, "headers"):
            self._http.headers.update(merged)
        else:  # 简易假会话无 headers：退化成每次请求都带
            self._default_headers = merged


    def get_filter_conditions(self, *, use_fallback: bool = True) -> FilterConditions:
        """拿齐 todo.md 列的 7 类筛选条件。

        策略：
          1. 条件接口 → 求职类型/薪资/经验/学历/规模（+3 个辅助维度）
          2. 城市接口 → 城市树 + 热点城市
          3. 公司行业 → 写死表（接口侧没有这张表，见 config）
          4. 任一步失败：``use_fallback`` 为真时用写死表补齐该步，
             并记进 ``FilterConditions.degraded``；为假则把异常抛出去。
        """
        degraded: dict[str, str] = {}
        parts: dict[str, Any] = {}

        try:
            parts.update(self.fetch_conditions())
        except FilterError as exc:
            if not use_fallback:
                raise
            logger.warning("筛选项接口不可用，改用写死表：%s", exc)
            fb = build_fallback_conditions()
            parts.update(
                job_types=fb.job_types,
                salaries=fb.salaries,
                experiences=fb.experiences,
                degrees=fb.degrees,
                scales=fb.scales,
                pay_types=fb.pay_types,
                stages=fb.stages,
                part_times=fb.part_times,
            )
            degraded["conditions"] = "fallback"

        try:
            parts.update(self.fetch_cities())
        except FilterError as exc:
            if not use_fallback:
                raise
            logger.warning("城市接口不可用，改用热点城市兜底：%s", exc)
            fb = build_fallback_conditions()
            parts.update(cities=fb.cities, hot_cities=fb.hot_cities)
            degraded["cities"] = "fallback"

        industries = load_industries()
        source = "fallback" if degraded else "api"
        return FilterConditions(
            cities=parts.get("cities", ()),
            hot_cities=parts.get("hot_cities", ()),
            job_types=parts.get("job_types", ()),
            salaries=parts.get("salaries", ()),
            experiences=parts.get("experiences", ()),
            degrees=parts.get("degrees", ()),
            scales=parts.get("scales", ()),
            industries=industries,
            pay_types=parts.get("pay_types", ()),
            stages=parts.get("stages", ()),
            part_times=parts.get("part_times", ()),
            source=source,
            degraded=degraded,
        )

    def fetch_conditions(self) -> dict[str, tuple]:
        """打条件接口，返回模型字段名 → 选项元组。

        主力路由是 ``pc/all/filter/conditions.json``；它 404/异常时依次降级到
        推荐页、搜索页那两份（字段子集不同，缺的维度不补）。
        """
        last_error: FilterError | None = None
        for key in ("conditions", "recommend_conditions", "search_condition"):
            path = self.endpoints.get(key)
            if not path:
                continue
            try:
                payload = self._get_json(path, action=f"条件接口[{key}]")
            except FilterError as exc:
                last_error = exc
                continue
            return parse_conditions_payload(payload)
        assert last_error is not None
        raise last_error

    def fetch_cities(self) -> dict[str, tuple]:
        """打城市接口，返回 ``cities``（省→市→区树）和 ``hot_cities``。"""
        payload = self._get_json(self.endpoints["city"], action="城市接口")
        data = _zpdata(payload)
        cities = tuple(
            CityNode.from_api(item) for item in (data.get("cityList") or []) if isinstance(item, dict)
        )
        hot = options_from_api(data.get("hotCityList"))
        if not cities and not hot:
            raise FilterDataError("城市接口返回里既没有 cityList 也没有 hotCityList")
        return {"cities": cities, "hot_cities": hot}

    def fetch_hot_cities(self) -> tuple[FilterOption, ...]:
        """只要热点城市（体量最小的那条路由）。"""
        payload = self._get_json(self.endpoints["hot_city"], action="热点城市接口")
        options = options_from_api(_zpdata(payload).get("hotCityList"))
        if not options:
            raise FilterDataError("热点城市接口返回里没有 hotCityList")
        return options

    def fetch_default_city(self) -> dict[str, Any]:
        """默认城市（站点当前城市）。原样返回 ``zpData.city``。"""
        payload = self._get_json(self.endpoints["default_city"], action="默认城市接口")
        return _zpdata(payload).get("city") or {}


    def _get_json(self, path: str, *, action: str = "") -> dict[str, Any]:
        url = self.base_url + path
        headers = getattr(self, "_default_headers", None)
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            if attempt:
                delay = self.backoff * (2 ** (attempt - 1))
                logger.debug("%s 第 %s 次重试，等待 %.1fs", action or path, attempt, delay)
                self._sleep(delay)
            try:
                raw = self._http.request(
                    "GET",
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = FilterTransportError(f"{action or path} 请求失败：{exc}")
                continue

            status = getattr(raw, "status_code", 0)
            if status >= 500:
                last_error = FilterTransportError(f"{action or path} 服务端错误 HTTP {status}")
                continue

            try:
                payload = raw.json()
            except ValueError as exc:
                last_error = FilterTransportError(f"{action or path} 响应不是 JSON：{exc}")
                continue

            if not isinstance(payload, dict):
                last_error = FilterDataError(f"{action or path} 响应不是 JSON 对象")
                continue

            code = _biz_code(payload)
            if code != C.CODE_OK:
                message = str(payload.get("message") or payload.get("msg") or "")
                raise FilterApiError(code, message or f"{action or path} 返回异常码 {code}", raw=payload)

            return payload

        raise last_error or FilterTransportError(f"{action or path} 请求失败")




def parse_conditions_payload(payload: Mapping[str, Any]) -> dict[str, tuple]:
    """把 ``conditions.json`` 的 ``zpData`` 映射成模型字段。

    线上形状::

        zpData: {jobTypeList, salaryList, experienceList, degreeList,
                 scaleList, payTypeList, stageList, partTimeList}

    ``search/job/condition.json`` 是子集（salary 无 low/high，有的版本没 stage），
    缺的键按空元组处理，不让它把整次请求拖垮。
    """
    data = _zpdata(payload)
    if not data:
        raise FilterDataError("条件接口返回的 zpData 为空")

    out: dict[str, tuple] = {}
    for api_key, model_key in C.CONDITION_LIST_FIELDS:
        out[model_key] = options_from_api(data.get(api_key))

    if not out["job_types"] and not out["salaries"] and not out["experiences"]:
        raise FilterDataError(f"条件接口的 zpData 里没有任何筛选项：{sorted(data)[:12]}")
    return out


def load_industries() -> tuple[IndustryGroup, ...]:
    """公司行业。

    **没有接口**（见 :mod:`boss_filter.config` 的说明），所以永远走写死表。
    这不是降级——是这条维度的唯一可用来源。
    """
    return tuple(
        IndustryGroup(name=name, options=options_from_rows(rows))
        for name, rows in tables.INDUSTRY_GROUPS
    )


def get_filter_conditions(
    *,
    base_url: str = C.BASE_URL,
    use_fallback: bool = True,
    html_path: str | Path | None = None,
    client: FilterClient | None = None,
) -> FilterConditions:
    """一步拿齐筛选条件。

    :param use_fallback: 接口挂掉时是否用写死表补齐
    :param html_path: 给定后优先解析这份已保存 HTML（完全不打接口），
        用于离线环境或校对写死表
    """
    if html_path is not None:
        return conditions_from_html(html_path)
    if not use_fallback and client is None:
        client = FilterClient(base_url=base_url)
    client = client or FilterClient(base_url=base_url)
    return client.get_filter_conditions(use_fallback=use_fallback)




def _zpdata(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("zpData")
    if data is None:
        data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _biz_code(payload: Mapping[str, Any]) -> int:
    raw = payload.get("code", payload.get("status", C.CODE_OK))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1
