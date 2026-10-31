"""tracemalloc 归因：把分配点按集成域名汇总。

两种视角：
- ``usage()``：当前**存活**分配的分布（一次快照即可，不需要等基线）；
- ``diff()``：与某次基线相比的**增量**（排查"谁在涨"）。

注意：``tracemalloc`` 是进程级全局状态，与 memspy 等工具会互相干扰，
同一时间只开一个。开启后 CPU/内存开销明显上升，排查完务必关闭。
"""
from __future__ import annotations

import logging
import time
import tracemalloc
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .activity import _domain_from_path

_LOGGER = logging.getLogger(__name__)

FRAMES = 4  # traceback 深度：4 帧足以定位到集成内部
DETAIL_TOP = 30
REFRESH_SECONDS = 60


def _fmt_trace(traceback_obj) -> str:
    """traceback → 保留最后两段路径 + 行号。"""
    parts = []
    for frame in list(traceback_obj)[:3]:
        path = str(frame.filename).replace("\\", "/")
        segs = [s for s in path.split("/") if s]
        tail = "/".join(segs[-2:]) if len(segs) >= 2 else path
        parts.append(f"{tail}:{frame.lineno}")
    return " <- ".join(parts)


def _domain_of(stat) -> str:
    """统计项 → 集成域名（取最内层分配点）。"""
    try:
        return _domain_from_path(stat.traceback[0].filename)
    except Exception:  # pylint: disable=broad-except
        return "unknown"


class MemoryTracer:
    """管理 tracemalloc 会话并按集成汇总。"""

    def __init__(self, hass: HomeAssistant) -> None:
        """Init."""
        self.hass = hass
        self._enabled = False
        self._baseline = None
        self._baseline_at: float | None = None
        self._unsub = None
        self._last_usage: dict[str, Any] = {"enabled": False}
        self._last_usage_at: float = 0.0
        #: 本工具开始观察 tracing 的时刻（用于解释"只有 0.x MB"这类读数）
        self._started_at: float = 0.0

    # ------------------------------------------------------------------ 状态
    @property
    def enabled(self) -> bool:
        """是否已开启 tracing。"""
        return self._enabled

    @property
    def last_usage(self) -> dict[str, Any]:
        """最近一次 usage 结果（供 sensor 读取，不触发计算）。"""
        return self._last_usage

    # ------------------------------------------------------------------ 开关
    def start(self) -> None:
        """开启 tracemalloc 与后台刷新。"""
        if self._enabled:
            return
        try:
            if not tracemalloc.is_tracing():
                tracemalloc.start(FRAMES)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("开启 tracemalloc 失败")
            return
        self._enabled = True
        self._started_at = time.time()
        self._baseline = None
        self._baseline_at = None
        self._unsub = async_track_time_interval(
            self.hass, self._refresh, timedelta(seconds=REFRESH_SECONDS)
        )
        _LOGGER.warning(
            "Diag Hub 已开启 tracemalloc(depth=%s)：CPU/内存开销会上升，排查完请关闭", FRAMES
        )
        self.hass.async_create_task(self._async_refresh())

    def stop(self) -> None:
        """关闭 tracing 并释放基线。"""
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("取消刷新订阅失败", exc_info=True)
            self._unsub = None
        if not self._enabled:
            return
        self._enabled = False
        self._started_at = 0.0
        self._baseline = None
        self._baseline_at = None
        self._last_usage = {"enabled": False}
        try:
            if tracemalloc.is_tracing():
                tracemalloc.stop()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("停止 tracemalloc 失败", exc_info=True)
        _LOGGER.warning("Diag Hub 已关闭 tracemalloc")

    # ------------------------------------------------------------------ 采样
    @callback
    def _refresh(self, _now=None) -> None:
        """定时刷新（间隔远大于单次耗时，不会重入堆积）。"""
        if self._enabled:
            self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self) -> None:
        """在 executor 里算一次 usage，结果缓存给 sensor。"""
        try:
            self._last_usage = await self.hass.async_add_executor_job(self.usage)
            self._last_usage_at = time.time()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("计算 tracemalloc usage 失败")

    # ------------------------------------------------------------------ 计算
    def usage(self) -> dict[str, Any]:
        """当前存活分配的按集成分布（阻塞，需在 executor 调用）。"""
        if not self._enabled or not tracemalloc.is_tracing():
            return {"enabled": False, "error": "tracemalloc 未开启"}

        snapshot = tracemalloc.take_snapshot()
        stats = snapshot.statistics("lineno")

        by_domain: dict[str, list[int]] = {}
        total_bytes = 0
        total_blocks = 0
        for stat in stats:
            domain = _domain_of(stat)
            slot = by_domain.setdefault(domain, [0, 0])
            slot[0] += stat.size
            slot[1] += stat.count
            total_bytes += stat.size
            total_blocks += stat.count

        rows = [
            {
                "domain": domain,
                "mb": round(size / 1048576, 2),
                "blocks": blocks,
                "percent": round(size / total_bytes * 100, 1) if total_bytes else 0,
            }
            for domain, (size, blocks) in by_domain.items()
        ]
        rows.sort(key=lambda item: item["mb"], reverse=True)

        return {
            "enabled": True,
            "mode": "usage",
            "total_mb": round(total_bytes / 1048576, 1),
            "total_blocks": total_blocks,
            "integrations": rows[:DETAIL_TOP],
            "top": [
                {
                    "trace": _fmt_trace(stat.traceback),
                    "kb": round(stat.size / 1024, 1),
                    "count": stat.count,
                }
                for stat in stats[:DETAIL_TOP]
            ],
            "elapsed_seconds": int(time.time() - self._last_usage_at)
            if self._last_usage_at
            else 0,
            # tracemalloc 已经观察了多久：刚开启时 total_mb 必然很小（只算新分配）
            "tracing_seconds": int(time.time() - self._started_at)
            if self._started_at
            else None,
        }

    def snap(self) -> dict[str, Any]:
        """建立基线（阻塞，需在 executor 调用）。"""
        if not self._enabled or not tracemalloc.is_tracing():
            return {"enabled": False, "error": "请先打开 switch.diag_hub_memory"}
        self._baseline = tracemalloc.take_snapshot()
        self._baseline_at = time.time()
        return {
            "enabled": True,
            "mode": "snap",
            "baseline_at": time.strftime("%H:%M:%S", time.localtime(self._baseline_at)),
        }

    def diff(self) -> dict[str, Any]:
        """与基线对比的增量，按集成汇总（阻塞，需在 executor 调用）。"""
        if not self._enabled or not tracemalloc.is_tracing():
            return {"enabled": False, "error": "请先打开 switch.diag_hub_memory"}
        if self._baseline is None:
            return {"enabled": True, "error": "没有基线，先调用 a=snap"}

        current = tracemalloc.take_snapshot()
        stats = current.compare_to(self._baseline, "lineno")

        by_domain: dict[str, int] = {}
        total_growth = 0
        for stat in stats:
            by_domain[_domain_of(stat)] = by_domain.get(_domain_of(stat), 0) + stat.size_diff
            total_growth += stat.size_diff

        rows = [
            {"domain": domain, "mb": round(size / 1048576, 2)}
            for domain, size in by_domain.items()
            if abs(size) >= 1024  # 去掉 1KB 以下的噪声
        ]
        rows.sort(key=lambda item: item["mb"], reverse=True)

        return {
            "enabled": True,
            "mode": "diff",
            "baseline_at": time.strftime("%H:%M:%S", time.localtime(self._baseline_at)),
            "window_seconds": int(time.time() - self._baseline_at),
            "total_mb": round(total_growth / 1048576, 2),
            "integrations": rows[:DETAIL_TOP],
            "top": [
                {
                    "trace": _fmt_trace(stat.traceback),
                    "kb": round(stat.size_diff / 1024, 1),
                    "count": stat.count_diff,
                }
                for stat in stats[:DETAIL_TOP]
            ],
        }
