"""诊断传感器：排行直接暴露成属性，方便放卡片或做自动化。

所有 sensor 只读缓存 / 纯内存聚合，不做重计算；
memory 的 snapshot 由后台 60 秒刷新一次，此处只读。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, TOP_N
from .runtime import DiagHubRuntime

SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors."""
    runtime: DiagHubRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EventsPerMinuteSensor(runtime),
            TopIntegrationSensor(runtime),
            TaskCountSensor(runtime),
            MemorySensor(runtime),
            ListenerSensor(runtime),
            LogSensor(runtime),
        ]
    )


class _DiagSensor(SensorEntity):
    """公共部分：每 30 秒取一次快照，多个属性读取共用同一份。"""

    _attr_should_poll = True

    def __init__(
        self, runtime: DiagHubRuntime, key: str, name: str, icon: str
    ) -> None:
        """Init."""
        self._runtime = runtime
        self._activity = runtime.activity
        self._snap: dict[str, Any] = {}
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = runtime.device_info
        self.entity_id = f"sensor.{DOMAIN}_{key}"

    async def async_update(self) -> None:
        """默认取活动快照（子类可覆盖）。"""
        # 进程指标走 executor 并节流，snapshot 只读缓存
        await self._activity.async_refresh_proc()
        self._snap = self._activity.snapshot(TOP_N)

    @property
    def _rows(self) -> list[dict[str, Any]]:
        return self._snap.get("integrations") or []


class EventsPerMinuteSensor(_DiagSensor):
    """全集成合计的状态写入速率（次/分钟）。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "events_per_min", "写入速率", "mdi:speedometer")

    @property
    def native_value(self) -> float:
        """按运行时长折算的写入速率。"""
        return float(self._snap.get("events_per_min", 0) or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """排行与进程指标。"""
        return {
            "enabled": self._snap.get("enabled", False),
            "events_total": self._snap.get("events_total", 0),
            # 运行不足 1 分钟时按总时长折算的速率会虚高，用这个标记区分
            "rate_stable": int(self._snap.get("uptime_seconds", 0)) >= 60,
            "uptime_seconds": self._snap.get("uptime_seconds", 0),
            "window_minutes": self._snap.get("window_minutes", 0),
            "top": [
                {
                    "domain": row["domain"],
                    "per_min": row["window_per_min"],
                    "events": row["window_events"],
                    "entities": row["entities"],
                    "tasks": row["tasks"],
                }
                for row in self._rows
            ],
            "proc": self._snap.get("proc", {}),
            "allocated_blocks": self._snap.get("allocated_blocks", 0),
        }


class TopIntegrationSensor(_DiagSensor):
    """最活跃的集成（按窗口内状态写入量）。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "top_integration", "最活跃集成", "mdi:chart-bar")

    @property
    def native_value(self) -> str:
        """最活跃的集成域名。"""
        rows = self._rows
        return rows[0]["domain"] if rows else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """前 N 名明细。"""
        return {
            "enabled": self._snap.get("enabled", False),
            "ranking": [
                {
                    "domain": row["domain"],
                    "per_min": row["window_per_min"],
                    "last_minute": row["last_minute"],
                    "entities": row["entities"],
                    "tasks": row["tasks"],
                }
                for row in self._rows
            ],
        }


class TaskCountSensor(_DiagSensor):
    """asyncio 任务总数与归属。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "tasks", "任务数", "mdi:sitemap")

    @property
    def native_value(self) -> int:
        """当前活跃任务总数。"""
        return int((self._snap.get("tasks") or {}).get("total", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """按集成 / 按任务名聚合（名字已归一化，便于发现同类任务堆积）。"""
        tasks = self._snap.get("tasks") or {}
        return {
            "enabled": self._snap.get("enabled", False),
            "by_domain": dict(tasks.get("by_domain") or []),
            "by_name": dict(tasks.get("by_name") or []),
        }


class MemorySensor(_DiagSensor):
    """tracemalloc 归因摘要：只读缓存。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "memory", "内存归因", "mdi:memory")

    async def async_update(self) -> None:
        """读 memory 的缓存（后台每 60 秒刷新）。"""
        self._snap = self._runtime.memory.last_usage or {}

    @property
    def native_value(self) -> str:
        """占用最大的集成域名；未开启时显示 off。"""
        rows = self._snap.get("integrations") or []
        return rows[0]["domain"] if rows else "off"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """按集成的内存分布与前几条分配点。"""
        return {
            "enabled": self._snap.get("enabled", False),
            "total_mb": self._snap.get("total_mb", 0),
            "total_blocks": self._snap.get("total_blocks", 0),
            "integrations": self._snap.get("integrations", [])[:TOP_N],
            "top": self._snap.get("top", [])[:10],
        }


class ListenerSensor(_DiagSensor):
    """事件总线监听器存量（按集成）。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "listeners", "监听器", "mdi:broadcast")

    async def async_update(self) -> None:
        """取 listener 快照（纯 dict 读取）。"""
        self._snap = self._runtime.listeners.snapshot()

    @property
    def native_value(self) -> int:
        """当前存活监听器总数。"""
        return int(self._snap.get("total_live", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """按集成的 live / peak / 累计注册次数。"""
        return {
            "enabled": self._snap.get("enabled", False),
            "patch_error": self._snap.get("patch_error", ""),
            "integrations": self._rows[:TOP_N],
            "by_event": (self._snap.get("by_event") or [])[:TOP_N],
        }


class LogSensor(_DiagSensor):
    """日志计数（按集成，warning/error 分开）。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "logs", "日志计数", "mdi:math-log")

    async def async_update(self) -> None:
        """取日志计数快照。"""
        self._snap = self._runtime.logs.snapshot()

    @property
    def native_value(self) -> int:
        """warning 及以上总条数。"""
        return int(self._snap.get("total", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """按集成的明细与最后一条摘要。"""
        return {
            "enabled": self._snap.get("enabled", False),
            "integrations": self._rows[:TOP_N],
        }
