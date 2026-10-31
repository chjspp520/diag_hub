"""按集成归属的活动统计。

设计约束（很重要，否则这个工具本身会变成下一个泄漏源）：
1. 所有时间序列用 ``deque(maxlen=...)`` 环形缓冲，永不无限追加；
2. 关闭时必须真正卸载：取消全部订阅、清空所有缓冲；
3. 状态回调里只做 dict 查找 + 自增，不做任何 I/O 或注册表查询（除首次缓存）；
4. 只观察，不 patch HA 内部接口，卸载后零残留。
"""
from __future__ import annotations

import asyncio
import heapq
import logging
import re
import time
from collections import deque
from datetime import timedelta
from typing import Any

from homeassistant.core import EVENT_STATE_CHANGED, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    HOT_ENTITY_N,
    HTTP_TOP_N,
    REGISTRY_CACHE_TTL,
    TASK_SAMPLE_SECONDS,
    WINDOW_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

_COMPONENT_MARKER = "custom_components/"
_UNKNOWN = "unknown"
_LONG_HEX = re.compile(r"[0-9a-f]{10,}")
#: 实体级计数上限：超过就只保留最忙的一半（长跑也不会无限增长）
_ENTITY_HITS_LIMIT = 4000


def _proc_status() -> dict[str, Any]:
    """读 /proc/self 的关键指标（非 Linux 时返回空）。"""
    out: dict[str, Any] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                for key in ("VmRSS", "VmSize", "VmPeak", "Threads"):
                    if line.startswith(key):
                        out[key.lower()] = line.split(":", 1)[1].strip()
    except OSError as err:
        out["error"] = str(err)
    return out


def _domain_from_path(filename: str) -> str:
    """把分配/执行位置的文件路径映射回集成域名。"""
    path = (filename or "").replace("\\", "/")
    idx = path.rfind(_COMPONENT_MARKER)
    if idx >= 0:
        seg = path[idx + len(_COMPONENT_MARKER):].split("/")[0]
        return seg or _UNKNOWN
    if "/homeassistant/" in path:
        return "homeassistant"
    if "/site-packages/" in path:
        return "pkg:" + path.split("/site-packages/")[-1].split("/")[0]
    return "other"


def _task_info(task: asyncio.Task) -> tuple[str, str]:
    """任务 → (所属集成, 归一化后的任务名)。"""
    coro: Any = None
    try:
        coro = task.get_coro()
    except Exception:  # pylint: disable=broad-except
        pass

    code = getattr(coro, "cr_code", None)
    if code is None:
        code = getattr(getattr(coro, "cr_frame", None), "f_code", None)
    filename = getattr(code, "co_filename", "") or ""
    domain = _domain_from_path(filename) if filename else _UNKNOWN

    name = ""
    try:
        name = task.get_name() or ""
    except Exception:  # pylint: disable=broad-except
        pass
    if not name or name.startswith("Task-"):
        name = getattr(code, "co_qualname", "") or getattr(coro, "__qualname__", "") or "?"
    # 把长 hex / 时间戳归一化，让同一类任务能聚合（例如 archive_retry_<uuid>）
    name = _LONG_HEX.sub("*", name.replace("\n", " "))
    return domain, name[:70]


class ActivityManager:
    """轻量活动统计：状态写入频率（按集成）+ task 归属 + 进程指标。"""

    def __init__(self, hass: HomeAssistant) -> None:
        """Init."""
        self.hass = hass
        self._enabled = False
        self._since: float = 0.0
        self._unsubs: list[Any] = []

        # entity_id → 集成域（缓存，registry 变更时清空）
        self._entity_domain: dict[str, str] = {}
        self._cache_at: float = 0.0

        # 每分钟一个桶，滚动 WINDOW_MINUTES 分钟
        self._minute = 0
        self._per_min: dict[str, deque[int]] = {}
        self._current: dict[str, int] = {}
        self._totals: dict[str, int] = {}
        self._events = 0

        # task 采样（覆盖式，不累积）
        self._task_total = 0
        self._task_by_domain: dict[str, int] = {}
        self._task_by_name: dict[str, int] = {}
        self._alloc_blocks = 0
        self._tick_count = 0
        # 每个集成的实体数（低频刷新）
        self._entity_count: dict[str, int] = {}
        # 谁在刷：实体级计数（有上限，防止长跑无限增长）
        self._entity_hits: dict[str, int] = {}

        # /proc/self/status 是阻塞读取，只能由 executor 刷新，snapshot 只读缓存
        self._proc: dict[str, Any] = {}
        self._proc_at: float = 0.0

    # ------------------------------------------------------------------ 开关
    @property
    def enabled(self) -> bool:
        """是否正在统计。"""
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """开/关统计。关闭时彻底卸载，回到零开销。"""
        if enabled == self._enabled:
            return
        if enabled:
            self._start()
        else:
            self._stop()

    def _start(self) -> None:
        """注册订阅。"""
        self._reset()
        self._minute = int(time.monotonic() // 60)
        self._since = time.time()
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed)
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=TASK_SAMPLE_SECONDS)
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry_updated
            )
        )
        self._enabled = True
        self._sample_tasks()
        # 实体数不要等第一个 60 秒周期，否则刚打开时排行里"实体"列全是 0
        self._sample_entity_counts()
        _LOGGER.info("Diag Hub 活动统计已启动")

    def _stop(self) -> None:
        """取消订阅并清空全部缓冲。"""
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("取消订阅失败", exc_info=True)
        self._unsubs.clear()
        self._enabled = False
        self._reset()
        _LOGGER.info("Diag Hub 活动统计已停止")

    def _reset(self) -> None:
        """清空所有统计状态（不含订阅）。"""
        self._entity_domain.clear()
        self._per_min.clear()
        self._current.clear()
        self._totals.clear()
        self._task_by_domain.clear()
        self._task_by_name.clear()
        self._entity_count.clear()
        self._entity_hits.clear()
        self._events = 0
        self._task_total = 0
        self._alloc_blocks = 0
        self._tick_count = 0

    # ------------------------------------------------------------------ 事件
    @callback
    def _on_state_changed(self, event) -> None:
        """状态变化：只做归属自增。"""
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return
        domain = self._entity_domain.get(entity_id)
        if domain is None:
            domain = self._resolve_domain(entity_id)
        hits = self._entity_hits.get(entity_id, 0) + 1
        self._entity_hits[entity_id] = hits
        self._bump(domain)

    @callback
    def _on_registry_updated(self, _event) -> None:
        """实体注册表变化：丢弃归属缓存，下次按新结果解析。"""
        self._entity_domain.clear()
        self._entity_count.clear()

    def _resolve_domain(self, entity_id: str) -> str:
        """查注册表拿到集成域，并写入缓存。"""
        now = time.monotonic()
        if now - self._cache_at > REGISTRY_CACHE_TTL and self._entity_domain:
            self._entity_domain.clear()
            self._cache_at = now
        try:
            entry = er.async_get(self.hass).async_get(entity_id)
        except Exception:  # pylint: disable=broad-except
            entry = None
        domain = (getattr(entry, "platform", None) if entry else None) or "yaml"
        self._entity_domain[entity_id] = domain
        return domain

    def _bump(self, domain: str) -> None:
        """计数 + 分钟滚动。"""
        minute = int(time.monotonic() // 60)
        if minute != self._minute:
            self._roll(minute)
        if domain not in self._per_min:
            self._per_min[domain] = deque([0] * WINDOW_MINUTES, maxlen=WINDOW_MINUTES)
        self._current[domain] = self._current.get(domain, 0) + 1
        self._totals[domain] = self._totals.get(domain, 0) + 1
        self._events += 1

    def _roll(self, minute: int) -> None:
        """把当前分钟计数推入所有域的环形缓冲。"""
        delta = minute - self._minute
        if delta <= 0:
            return
        steps = min(delta, WINDOW_MINUTES)
        for domain, dq in self._per_min.items():
            for _ in range(steps):
                dq.append(self._current.get(domain, 0))
        for domain in self._current:
            self._current[domain] = 0
        self._minute = minute
        if len(self._entity_hits) > _ENTITY_HITS_LIMIT:
            top = heapq.nlargest(
                _ENTITY_HITS_LIMIT // 2, self._entity_hits.items(), key=lambda kv: kv[1]
            )
            self._entity_hits = dict(top)

    # ------------------------------------------------------------------ 采样
    @callback
    def _tick(self, _now=None) -> None:
        """定时采样：task 归属 + 每 60 秒刷新实体数/allocated blocks。"""
        self._sample_tasks()
        self._tick_count += 1
        if self._tick_count % 6 == 0:
            self._sample_entity_counts()
        try:
            import sys  # noqa: PLC0415

            self._alloc_blocks = sys.getallocatedblocks()
        except Exception:  # pylint: disable=broad-except
            self._alloc_blocks = 0

    def _sample_tasks(self) -> None:
        """遍历 asyncio 任务，按集成与任务名聚合。"""
        try:
            tasks = asyncio.all_tasks(self.hass.loop)
        except RuntimeError:
            return
        by_domain: dict[str, int] = {}
        by_name: dict[str, int] = {}
        for task in tasks:
            domain, name = _task_info(task)
            by_domain[domain] = by_domain.get(domain, 0) + 1
            by_name[name] = by_name.get(name, 0) + 1
        self._task_total = len(tasks)
        self._task_by_domain = by_domain
        self._task_by_name = by_name

    def _sample_entity_counts(self) -> None:
        """统计每个集成当前的实体数（走同一份归属缓存）。"""
        counts: dict[str, int] = {}
        for state in self.hass.states.async_all():
            entity_id = state.entity_id
            domain = self._entity_domain.get(entity_id)
            if domain is None:
                domain = self._resolve_domain(entity_id)
            counts[domain] = counts.get(domain, 0) + 1
        self._entity_count = counts

    # ------------------------------------------------------------------ 进程指标
    async def async_refresh_proc(self, min_interval: float = 5.0) -> dict[str, Any]:
        """刷新 /proc/self/status 缓存（阻塞读取走 executor，按间隔节流）。

        页面每 5 秒轮询、sensor 每 30 秒刷新，多个消费者共用这一份缓存；
        snapshot() 只读缓存，绝不在事件循环里 open()。
        """
        now = time.monotonic()
        if self._proc and now - self._proc_at < min_interval:
            return self._proc
        try:
            data = await self.hass.async_add_executor_job(_proc_status)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("读取 /proc/self/status 失败", exc_info=True)
            data = {"error": "读取失败"}
        self._proc = data
        self._proc_at = now
        return data

    # ------------------------------------------------------------------ 读取
    def snapshot(
        self, top_n: int = HTTP_TOP_N, hot_n: int = HOT_ENTITY_N
    ) -> dict[str, Any]:
        """返回完整快照（HTTP 与 sensor 共用）。

        top_n 只裁剪「按集成」的排行；实体级热度用 hot_n 单独控制，
        因为页面要能滚着看更多实体，而 sensor 属性另有 16KB 限制。
        """
        if self._enabled:
            minute = int(time.monotonic() // 60)
            if minute != self._minute:
                self._roll(minute)

        rows: list[dict[str, Any]] = []
        for domain, dq in self._per_min.items():
            last = self._current.get(domain, 0)
            window = sum(dq) + last
            rows.append(
                {
                    "domain": domain,
                    "last_minute": last,
                    "window_events": window,
                    "window_per_min": round(window / WINDOW_MINUTES, 2),
                    "total": self._totals.get(domain, 0),
                    "per_min": list(dq)[-15:],
                    "entities": self._entity_count.get(domain, 0),
                    "tasks": self._task_by_domain.get(domain, 0),
                }
            )
        rows.sort(key=lambda item: item["window_events"], reverse=True)

        return {
            "enabled": self._enabled,
            "since": self._since,
            "uptime_seconds": int(time.time() - self._since) if self._since else 0,
            "window_minutes": WINDOW_MINUTES,
            "events_total": self._events,
            "events_per_min": round(self._events / max(1, int(time.time() - self._since) / 60), 2)
            if self._since
            else 0,
            "integrations": rows[:top_n],
            "integration_count": len(rows),
            "hot_entity_count": len(self._entity_hits),
            # 谁在刷：实体级热度（累计值，按 hot_n 裁剪，页面可滚动查看）
            "hot_entities": [
                {
                    "entity": entity_id,
                    "domain": self._entity_domain.get(entity_id, _UNKNOWN),
                    "hits": hits,
                }
                for entity_id, hits in heapq.nlargest(
                    min(hot_n, len(self._entity_hits)),
                    self._entity_hits.items(),
                    key=lambda kv: kv[1],
                )
            ],
            "tasks": {
                "total": self._task_total,
                "by_domain": sorted(
                    self._task_by_domain.items(), key=lambda kv: -kv[1]
                )[:top_n],
                "by_name": sorted(self._task_by_name.items(), key=lambda kv: -kv[1])[:top_n],
            },
            "allocated_blocks": self._alloc_blocks,
            # 只读缓存：真实读取在 async_refresh_proc() 里走 executor
            "proc": self._proc,
        }
