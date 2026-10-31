"""运行时容器：把各追踪器收在一起，便于统一开关与卸载。"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from .activity import ActivityManager
from .entity import build_device_info
from .listeners import ListenerTracker
from .logs import LogTracker
from .memory import MemoryTracer


class DiagHubRuntime:
    """一个 config entry 对应的全部追踪器。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | None = None) -> None:
        """Init."""
        self.hass = hass
        self.entry = entry
        self.device_info: DeviceInfo | None = (
            build_device_info(hass, entry) if entry is not None else None
        )
        self.activity = ActivityManager(hass)
        self.memory = MemoryTracer(hass)
        self.listeners = ListenerTracker(hass)
        self.logs = LogTracker(hass)

    def shutdown(self) -> None:
        """卸载：全部停止并释放（顺序无关，每个都是幂等的）。"""
        self.activity.set_enabled(False)
        self.memory.stop()
        self.listeners.stop()
        self.logs.stop()
