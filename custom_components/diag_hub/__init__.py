"""Diag Hub：按集成归属的内存 / CPU 负载诊断（开发期自检用）。

四块能力，各自独立开关，关闭即完全停止采集：
- activity：谁在刷状态、谁在漏 task（轻量，可常开）
- memory：tracemalloc 归因，按集成汇总内存（重，仅排查时开）
- listeners：谁注册了事件监听器不注销
- logs：谁在刷 warning / error

数据在设备页「访问」里看：所有实体挂在一个设备上，设备的访问地址指向
自建诊断页面，前端模块会把它接管成悬浮窗（不占用侧边栏）。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import DEVICE_NAME
from .http_api import DiagHubView
from .page import async_link_device, async_setup_page
from .runtime import DiagHubRuntime
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "switch"]

# HomeAssistantView 一旦注册无法注销，用模块级标记避免重载时重复注册
_VIEW_REGISTERED = False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Diag Hub."""
    runtime = DiagHubRuntime(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime

    global _VIEW_REGISTERED  # noqa: PLW0603
    if not _VIEW_REGISTERED:
        hass.http.register_view(DiagHubView(hass))
        _VIEW_REGISTERED = True

    async_register_services(hass)
    if entry.title != DEVICE_NAME:
        # 早期版本条目标题是英文，统一成中文（与设备名一致）
        hass.config_entries.async_update_entry(entry, title=DEVICE_NAME)

    # 诊断页面 + 悬浮窗模块（全局只注册一次，顺手清掉旧版仪表盘）
    await async_setup_page(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _schedule_device_link(hass, entry)
    _LOGGER.debug("Diag Hub 已加载（各追踪器默认关闭，由 switch 控制）")
    return True


def _schedule_device_link(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """HA 启动完成后把设备「访问」指向诊断页面（best-effort）。

    设备由实体创建、绝对地址要等 network 就绪，所以放到 STARTED 之后；
    已经跑起来（reload 集成）时直接补一次。
    """

    async def _link(_event: object | None = None) -> None:
        try:
            await async_link_device(hass, entry.entry_id)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("写设备「访问」链接失败", exc_info=True)

    if hass.is_running:
        hass.async_create_task(_link())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _link)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Diag Hub."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        runtime: DiagHubRuntime | None = hass.data.get(DOMAIN, {}).pop(
            entry.entry_id, None
        )
        if runtime is not None:
            runtime.shutdown()
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
    return unload_ok
