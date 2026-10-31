"""实体公共部分：把所有实体挂到同一个设备上。

两个作用：
- HA 的「集成条目」页只平铺列实体，看不到实体类型；有了设备之后，设备页会按
  传感器 / 开关分组显示；
- 设备的 ``configuration_url`` 指向自建诊断页面，设备页右上角就有「访问」，
  点开时由前端模块接管，弹出悬浮窗（见 page.py）。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN
from .page import page_url

_LOGGER = logging.getLogger(__name__)

DEVICE_NAME = "集成诊断"
MANUFACTURER = "Diag Hub"
MODEL = "开发期诊断"


def build_device_info(hass: HomeAssistant, entry: ConfigEntry) -> DeviceInfo:
    """构造设备信息：一个 config entry 对应一个设备。"""
    info: DeviceInfo = {
        "identifiers": {(DOMAIN, entry.entry_id)},
        "name": DEVICE_NAME,
        "manufacturer": MANUFACTURER,
        "model": MODEL,
        # SERVICE：没有真实硬件，设备页只显示实体与左上角「访问」
        "entry_type": DeviceEntryType.SERVICE,
    }
    # setup 早期可能还拿不到绝对地址（internal_url 未配置、network 未就绪），
    # 这时先不写；HA 启动完成后 __init__ 会把链接补上。
    url = page_url(hass)
    if url:
        info["configuration_url"] = url
    return info
