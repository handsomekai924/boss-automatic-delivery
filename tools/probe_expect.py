"""P3 实测校准：把「求职期望流」五个待定点一次打清楚（见 docs/job-expect-flow-plan.md §七）。

只打**只读列表**接口（expect/list、recommend/job/list、search/joblist），不发消息、
不改账号状态。每次探针都把原始 code / zpData 顶层键打出来，方便对着注释回填。

用法::

    python tools/probe_expect.py                 # 全套五点
    python tools/probe_expect.py --only auth     # 只打第 1 点（expect/list 鉴权）
    python tools/probe_expect.py --skip-stoken   # 别拉 Chrome，只用库里已有的令牌

输出是人读的探针报告；要 JSON 加 --json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_jobs import create_client  # noqa: E402
from boss_jobs.errors import JobApiError, JobError  # noqa: E402


def _brief(payload: Any, *, keys: int = 12) -> dict[str, Any]:
    """把响应压成「code / message / zpData 顶层键 + 各列表长度」，别把整包打出来。"""
    if not isinstance(payload, dict):
        return {"raw_type": type(payload).__name__}
    out: dict[str, Any] = {"code": payload.get("code"), "message": payload.get("message")}
    zp = payload.get("zpData")
    if isinstance(zp, dict):
        out["zpData_keys"] = sorted(zp.keys())[:keys]
        for name in ("mixExpect", "expectList", "partTimeExpectList", "jobList", "hasMore"):
            if name in zp:
                value = zp[name]
                if isinstance(value, list):
                    out[name] = f"list[{len(value)}]"
                elif isinstance(value, dict):
                    out[name] = "dict" + str(sorted(value.keys())[:10])
                else:
                    out[name] = repr(value)[:80]
    else:
        out["zpData"] = repr(zp)[:120]
    return out


def _sample_items(payload: Any, name: str, limit: int = 3) -> list[dict[str, Any]]:
    zp = (payload or {}).get("zpData") if isinstance(payload, dict) else None
    if not isinstance(zp, dict):
        return []
    raw = zp.get(name)
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    rows = []
    for item in raw[:limit]:
        if isinstance(item, dict):
            rows.append({k: item.get(k) for k in sorted(item.keys()) if k in {
                "encryptId", "encryptExpectId", "mixExpectType", "expectType",
                "positionName", "position", "locationName", "location", "tagName",
            } or k.lower().endswith("id")})
    return rows


class Probe:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.findings: list[str] = []

    def note(self, text: str) -> None:
        self.findings.append(text)
        print(f"  → {text}")

    def record(self, point: str, **kwargs: Any) -> None:
        row = {"point": point, **kwargs}
        self.rows.append(row)
        print(f"  [{point}] {json.dumps(kwargs, ensure_ascii=False)[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="P3 实测校准探针（只读）")
    parser.add_argument("--only", help="只打某一点：auth / ids / searchctx / parttime / method")
    parser.add_argument("--skip-stoken", action="store_true", help="不拉 Chrome，只用库里已有的令牌")
    parser.add_argument("--json", action="store_true", help="末尾输出 JSON")
    args = parser.parse_args()

    probe = Probe()
    t0 = time.perf_counter()
    client = create_client()
    if args.skip_stoken:
        client.stoken_provider = None

    http = client._http
    print(f"会话已装配，base={client.base_url}")

    def strip_cookie(name: str) -> None:
        """同名 cookie 先清再写的老坑：这里要**摘掉**令牌，得按名清干净。"""
        jar = getattr(http, "cookies", None)
        if jar is None:
            return
        for cookie in list(jar):
            if getattr(cookie, "name", None) == name:
                try:
                    jar.clear(cookie.domain or "", cookie.path or "/", name)
                except Exception:  # noqa: BLE001
                    pass

    def want(name: str) -> bool:
        return args.only in (None, name)

    # ------------------------------------------------------------------ #
    # 点 1 + 2：expect/list.json 的鉴权 & 三类期望的真实字段名
    # ------------------------------------------------------------------ #
    if want("auth") or want("ids"):
        print("\n=== 点 1/2：GET /pc/recommend/expect/list.json ===")
        path = client.endpoints["job_expect_list"]
        # 先**不带** __zp_stoken__ 打一发（点 1）
        from boss_jobs import STOKEN_COOKIE
        saved_stoken = client._get_cookie(STOKEN_COOKIE)
        saved_provider = client.stoken_provider
        strip_cookie(STOKEN_COOKIE)
        client.stoken_provider = None  # 别让 _ensure_stoken 又给塞回去
        try:
            raw = client._get_json(path, params={}, action="探针-期望列表-无令牌")
            probe.record("auth/no-stoken", **_brief(raw))
            if isinstance(raw, dict) and raw.get("code") == 37:
                probe.note("点1：expect/list 也要 __zp_stoken__（回 code 37）→ fetch_expect_list 要换 stoken 重试")
            elif isinstance(raw, dict) and raw.get("code") == 0:
                probe.note("点1：expect/list 只要登录 Cookie（无令牌也 code 0）→ 现状裸 _get_json 正确")
            else:
                probe.note(f"点1：无令牌回了别的码 {raw.get('code') if isinstance(raw, dict) else '?'}，再看")
        except JobApiError as exc:
            probe.record("auth/no-stoken", error=str(exc), code=getattr(exc, "code", None))
            probe.note(f"点1：无令牌抛 JobApiError code={getattr(exc, 'code', None)}")
        except JobError as exc:
            probe.record("auth/no-stoken", error=str(exc))
            probe.note(f"点1：无令牌抛 {type(exc).__name__}")

        # 带令牌再打一发
        if saved_stoken:
            from boss_jobs.stoken import put_cookie
            put_cookie(getattr(http, "cookies", None), STOKEN_COOKIE, saved_stoken)
        client.stoken_provider = saved_provider
        try:
            expects = client.fetch_expect_list()
            payload_seen = client._get_json(path, params={}, action="探针-期望列表-再取原始")
            probe.record(
                "ids/raw",
                **_brief(payload_seen),
                sample_mix=_sample_items(payload_seen, "mixExpect"),
                sample_expectList=_sample_items(payload_seen, "expectList"),
                sample_partTime=_sample_items(payload_seen, "partTimeExpectList"),
                parsed={
                    "mix": expects.mix and expects.mix.encrypt_expect_id,
                    "expects": [e.encrypt_expect_id for e in expects.expects],
                    "part_time": [e.encrypt_expect_id for e in expects.part_time],
                },
            )
            if expects.mix or expects.expects or expects.part_time:
                probe.note("点2：三类期望解析到了，对照上面 raw 里的 encryptId / encryptExpectId 键名")
            else:
                probe.note("点2：本账号期望列表是空的（文档记过这种情况）——字段名对称性还是靠防御性双键名兜着")
        except JobError as exc:
            probe.record("ids/raw", error=str(exc), code=getattr(exc, "code", None))
            probe.note(f"点2：拉期望列表失败 {exc}")

    # ------------------------------------------------------------------ #
    # 点 5：期望流 / 搜索流 GET vs POST
    # ------------------------------------------------------------------ #
    if want("method"):
        print("\n=== 点 5：GET vs POST ===")
        print("  （GET 走生产的 stoken 重试；裸 _request_json 不 _ensure_stoken，会把 37 误判成「方法不行」）")
        for label, key in (("期望流", "job_expect_flow"), ("搜索流", "job_search")):
            path = client.endpoints[key]
            params = {"page": "1", "pageSize": "5"}
            if key == "job_expect_flow":
                params.update({"encryptExpectId": "PROBE", "jobType": "1903"})
            else:
                params.update({"query": "python", "scene": "1"})
            # GET：走生产的 stoken 重试（先 _ensure_stoken，撞 37 还会退避换新）
            try:
                payload = client._get_json_with_stoken_retry(
                    path, params=params, action=f"探针-{label}-GET",
                )
                probe.record(f"method/{key}/GET", **_brief(payload))
            except Exception as exc:  # noqa: BLE001
                probe.record(f"method/{key}/GET", error=f"{type(exc).__name__}: {exc}")
            # POST：同一枚令牌，只为确认服务端认不认 POST
            try:
                payload = client._request_json(
                    "POST", path, params=params, data=params,
                    action=f"探针-{label}-POST",
                )
                probe.record(f"method/{key}/POST", **_brief(payload))
            except Exception as exc:  # noqa: BLE001
                probe.record(f"method/{key}/POST", error=f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # 点 3 + 4：期望流 / 搜索流的真实参数（有期望 id 才有意义）
    # ------------------------------------------------------------------ #
    if want("searchctx") or want("parttime"):
        print("\n=== 点 3/4：期望上下文与兼职参数（需要库里有期望 id）===")
        try:
            expects = client.fetch_expect_list()
        except JobError as exc:
            probe.note(f"点3/4：拉不到期望列表，跳过（{exc}）")
            expects = None

        if expects is not None:
            pt = expects.part_time[0] if expects.part_time else None
            ex = expects.expects[0] if expects.expects else None
            mix = expects.mix

            if want("parttime") and pt is not None:
                print(f"\n-- 点4：兼职期望 {pt.encrypt_expect_id} --")
                for extra in ({}, {"partTimeExpect": pt.encrypt_expect_id}):
                    params = {
                        "page": "1", "pageSize": "5",
                        "jobType": "1903",
                        "encryptExpectId": pt.encrypt_expect_id,
                        **extra,
                    }
                    try:
                        # 走生产路径的 stoken 重试——裸 _get_json 不 _ensure_stoken，
                        # 会把「缺令牌」误判成「参数不行」
                        payload = client._get_json_with_stoken_retry(
                            client.endpoints["job_expect_flow"], params=params,
                            action=f"探针-兼职-{sorted(extra)}",
                        )
                        n_jobs = len((payload.get("zpData") or {}).get("jobList") or []) if isinstance(payload, dict) else 0
                        probe.record(f"parttime/{'with' if extra else 'only'}", **_brief(payload), n_jobs=n_jobs)
                    except JobApiError as exc:
                        probe.record(f"parttime/{'with' if extra else 'only'}", code=getattr(exc, "code", None), error=str(exc))
                    except JobError as exc:
                        probe.record(f"parttime/{'with' if extra else 'only'}", error=str(exc))
                    time.sleep(1.0)  # 隔开两发，免得被安全网关当过频连环 37
                probe.note("点4：上面 with/only 两发对比——若 only 就 code 0 且有职位，服务端不要 partTimeExpect")

            if want("searchctx") and (ex is not None or mix is not None):
                eid = (ex or mix).encrypt_expect_id
                print(f"\n-- 点3：搜索 + 期望上下文 {eid} --")
                seen: dict[str, list[str]] = {}
                for label, params in (
                    ("no-ctx", {"query": "python", "city": "101280100", "scene": "1", "page": "1", "pageSize": "10"}),
                    ("with-ctx", {"query": "python", "city": "101280100", "scene": "1", "page": "1", "pageSize": "10",
                                  "encryptExpectId": eid}),
                ):
                    try:
                        payload = client._get_json_with_stoken_retry(
                            client.endpoints["job_search"], params=params,
                            action=f"探针-搜索-{label}",
                        )
                        jobs = (payload.get("zpData") or {}).get("jobList") or []
                        ids = [j.get("encryptJobId") for j in jobs if isinstance(j, dict)]
                        seen[label] = ids
                        probe.record(f"searchctx/{label}", **_brief(payload), job_ids=ids[:10])
                    except JobApiError as exc:
                        probe.record(f"searchctx/{label}", code=getattr(exc, "code", None), error=str(exc))
                    except JobError as exc:
                        probe.record(f"searchctx/{label}", error=str(exc))
                    time.sleep(1.0)  # 隔开两发，免得被安全网关当过频连环 37
                if seen.get("no-ctx") and seen.get("with-ctx"):
                    a, b = seen["no-ctx"], seen["with-ctx"]
                    overlap = set(a) & set(b)
                    if overlap == set(a) == set(b):
                        probe.note("点3：带不带 encryptExpectId 结果集一样 → 更像「个性化信号」，不是范围限定")
                    elif not overlap:
                        probe.note("点3：结果集完全不相交 → 像是「限定在该期望内搜」")
                    else:
                        probe.note(f"点3：结果集部分重叠（{len(overlap)}/{len(a)}）→ 可能是排序/个性化加权，不是硬过滤")

    print(f"\n=== 探针结束，耗时 {time.perf_counter() - t0:.1f}s ===")
    print("\n".join(f"· {line}" for line in probe.findings) or "· 无结论")
    if args.json:
        print(json.dumps({"rows": probe.rows, "findings": probe.findings}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
