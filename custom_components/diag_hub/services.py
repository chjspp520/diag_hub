"""服务：刷新设备页「访问」链接。

诊断信息通过设备页「访问」打开（前端会接管成悬浮窗），链接是绝对 URL，
写死在设备注册表里。改过 HA 的访问地址（internal_url / 反向代理 / 端口映射）
之后调一下这个服务即可，不用重启。
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .page import async_link_device

_LOGGER = logging.getLogger(__name__)

SERVICE_RELINK_DEVICE = "relink_device"

_SCHEMA = vol.Schema({})


async def _async_handle_relink_device(call: ServiceCall) -> None:
    """重算访问地址并写回设备（best-effort，不向调用方抛异常）。"""
    linked = 0
    for entry_id in list(call.hass.data.get(DOMAIN, {})):
        try:
            if await async_link_device(call.hass, entry_id):
                linked += 1
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("刷新设备链接失败：%s", entry_id, exc_info=True)
    _LOGGER.debug("relink_device 完成，已更新 %s 个条目", linked)


def async_register_services(hass: HomeAssistant) -> None:
    """注册服务（幂等，reload 不会重复注册）。"""
    if hass.services.has_service(DOMAIN, SERVICE_RELINK_DEVICE):
        return
    hass.services.async_register(
        DOMAIN,
        SERVICE_RELINK_DEVICE,
        _async_handle_relink_device,
        schema=_SCHEMA,
    )
