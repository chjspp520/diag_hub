"""开关：四块能力各自独立开关，关掉即完全停止。

- activity  轻量、可常开，重启后自动恢复
- memory    重（tracemalloc），不恢复
- listeners 轻（包装事件总线），不恢复
- logs      轻（root logger 计数），不恢复

四个开关的 ``is_on`` 一律直接读追踪器自身的 ``enabled``，不缓存副本：
这样启动失败时界面立刻回到 off，并弹出真实原因（HomeAssistantError），
不会再出现「点开 2 秒后自己关掉、却没有提示」的情况。
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .runtime import DiagHubRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switches."""
    runtime: DiagHubRuntime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ActivitySwitch(runtime),
            MemorySwitch(runtime),
            ListenerSwitch(runtime),
            LogSwitch(runtime),
        ]
    )


class _BaseSwitch(SwitchEntity):
    """公共部分：不做轮询，状态由追踪器自身维护。"""

    _attr_should_poll = False

    def __init__(self, runtime: DiagHubRuntime, key: str, name: str, icon: str) -> None:
        """Init."""
        self._runtime = runtime
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = runtime.device_info
        # 实体 ID 固定，不随名称变化
        self.entity_id = f"switch.{DOMAIN}_{key}"
        self._attr_is_on = False

    @property
    def is_on(self) -> bool:
        """子类覆盖：直接读追踪器的真实状态。"""
        return self._attr_is_on


class ActivitySwitch(RestoreEntity, _BaseSwitch):
    """活动统计总开关（状态写入频率 + task 归属）。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init（显式指定基类，避免 MRO 把参数传给 RestoreEntity）。"""
        _BaseSwitch.__init__(self, runtime, "activity", "活动统计", "mdi:pulse")

    @property
    def is_on(self) -> bool:
        """是否正在统计。"""
        return self._runtime.activity.enabled

    async def async_added_to_hass(self) -> None:
        """恢复上次状态（重启后接着统计，开销极低）。"""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            self._runtime.activity.set_enabled(True)
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """开始统计。"""
        self._runtime.activity.set_enabled(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """停止统计并完全卸载。"""
        self._runtime.activity.set_enabled(False)
        self.async_write_ha_state()


class MemorySwitch(_BaseSwitch):
    """tracemalloc 归因开关。

    刻意不恢复状态：tracing 会显著抬高 CPU/内存，重启后自动重开容易忘了关。
    """

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "memory", "内存追踪", "mdi:memory")

    @property
    def is_on(self) -> bool:
        """是否正在 tracing。"""
        return self._runtime.memory.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """开启 tracemalloc。"""
        self._runtime.memory.start()
        self.async_write_ha_state()
        if not self._runtime.memory.enabled:
            raise HomeAssistantError("开启内存追踪失败：tracemalloc 启动失败，详见日志")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """关闭 tracemalloc 并清基线。"""
        self._runtime.memory.stop()
        self.async_write_ha_state()


class ListenerSwitch(_BaseSwitch):
    """事件总线监听器归属开关。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "listeners", "监听器统计", "mdi:broadcast")

    @property
    def is_on(self) -> bool:
        """是否已包装事件总线。"""
        return self._runtime.listeners.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """包装失败时把原因露出来，方便判断为什么打不开。"""
        return {"patch_error": self._runtime.listeners.patch_error}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """开始包装事件总线；失败就把原因抛给界面。"""
        self._runtime.listeners.start()
        self.async_write_ha_state()
        if not self._runtime.listeners.enabled:
            reason = self._runtime.listeners.patch_error or "未知原因"
            raise HomeAssistantError(f"监听器统计启动失败：{reason}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """还原事件总线。"""
        self._runtime.listeners.stop()
        self.async_write_ha_state()


class LogSwitch(_BaseSwitch):
    """日志计数开关。"""

    def __init__(self, runtime: DiagHubRuntime) -> None:
        """Init."""
        super().__init__(runtime, "logs", "日志计数", "mdi:math-log")

    @property
    def is_on(self) -> bool:
        """是否已挂载计数器。"""
        return self._runtime.logs.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """挂载计数器。"""
        self._runtime.logs.start()
        self.async_write_ha_state()
        if not self._runtime.logs.enabled:
            raise HomeAssistantError("挂载日志计数器失败，详见日志")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """移除计数器。"""
        self._runtime.logs.stop()
        self.async_write_ha_state()
