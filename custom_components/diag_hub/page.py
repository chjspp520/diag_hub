"""诊断页面 + 前端悬浮窗（不再使用 Lovelace 仪表盘 / 侧边栏条目）。

做法参考 esxi_control：

1. ``DiagPageView`` 提供自建 HTML 页面（``www/page.html``），数据直接拉
   ``/api/diag_hub?a=all``，不依赖 Lovelace；
2. ``DiagEmbedView`` 提供注入 HA 前端的小模块（``www/frontend/diag-embed.js``），
   通过 ``frontend.add_extra_js_url`` 注入。设备页「访问」被点击时拦下默认跳转，
   在 HA 界面内弹出一个可拖动 / 可缩放的悬浮窗，窗内用 iframe 加载上面的页面；
3. 设备的 ``configuration_url`` 指向这个页面，所以设备页右上角就有「访问」。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

#: 页面路径（必须与 www/frontend/diag-embed.js 里的 PAGE_PATH 一致）
PAGE_PATH = "/diag_hub/page"
STATIC_PREFIX = "/diag_hub"
EMBED_PATH = f"{STATIC_PREFIX}/frontend/diag-embed.js"

#: 0.2 / 0.3 版本建过的 Lovelace 仪表盘 url_path，启动时清理掉
LEGACY_DASHBOARD_PATH = "diag-hub"

_DATA_KEY = f"{DOMAIN}_page"

_WWW = Path(__file__).parent / "www"
_PAGE_FILE = _WWW / "page.html"
_EMBED_FILE = _WWW / "frontend" / "diag-embed.js"

#: mtime → 文本 的小缓存，改完文件不用重启也能生效
_CACHE: dict[str, tuple[float, str]] = {}


def _read_cached(path: Path) -> str:
    """按 mtime 缓存地读取文本（阻塞，应在 executor 里调用）。"""
    mtime = path.stat().st_mtime
    key = str(path)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    text = path.read_text(encoding="utf-8")
    _CACHE[key] = (mtime, text)
    return text


class DiagPageView(HomeAssistantView):
    """诊断 HTML 页面。

    ``requires_auth = False``：与 /api/diag_hub 口径一致（内网排查工具），
    这样设备页「访问」点开即可用，无需处理签名票据。
    """

    requires_auth = False
    url = PAGE_PATH
    name = "diag_hub:page"

    async def get(self, request: web.Request) -> web.Response:
        """返回页面 HTML。"""
        try:
            text = await request.app["hass"].async_add_executor_job(
                _read_cached, _PAGE_FILE
            )
        except OSError:
            _LOGGER.exception("诊断页面文件缺失：%s", _PAGE_FILE)
            return web.Response(
                text="page.html 缺失，请重新部署 diag_hub/www/",
                status=500,
                content_type="text/plain",
            )
        return web.Response(
            text=text,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )


class DiagEmbedView(HomeAssistantView):
    """悬浮窗前端模块（白名单，只放行这一个文件）。"""

    requires_auth = False
    url = STATIC_PREFIX + "/frontend/{file_name}"
    name = "diag_hub:embed"

    async def get(self, request: web.Request, file_name: str) -> web.Response:
        """返回 JS 模块。"""
        if file_name != _EMBED_FILE.name:
            return web.Response(status=404)
        try:
            text = await request.app["hass"].async_add_executor_job(
                _read_cached, _EMBED_FILE
            )
        except OSError:
            _LOGGER.exception("悬浮窗模块缺失：%s", _EMBED_FILE)
            return web.Response(status=404)
        return web.Response(
            text=text,
            content_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )


def page_url(hass: HomeAssistant) -> str | None:
    """设备页「访问」用的绝对地址（设备注册表只接受绝对 URL）。"""
    base = hass.config.internal_url or hass.config.external_url
    if not base:
        try:
            from homeassistant.helpers.network import get_url  # noqa: PLC0415

            base = get_url(hass, allow_ip=True, allow_cloud=False)
        except Exception:  # pylint: disable=broad-except
            base = ""
    if not base:
        api = getattr(hass.config, "api", None)
        base = getattr(api, "base_url", None) or ""
    if not base:
        return None

    url = f"{base.rstrip('/')}{PAGE_PATH}"
    try:
        from homeassistant.helpers import config_validation as cv  # noqa: PLC0415

        return cv.url(url)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("诊断页面直链不合法，先不放「访问」按钮：%s", url)
        return None


async def async_setup_page(hass: HomeAssistant) -> None:
    """注册页面视图、注入悬浮窗模块（全局只做一次），并清理旧仪表盘。"""
    store = hass.data.setdefault(_DATA_KEY, {})

    if not store.get("views"):
        hass.http.register_view(DiagPageView)
        hass.http.register_view(DiagEmbedView)
        store["views"] = True

        try:
            from homeassistant.components import frontend as ha_frontend  # noqa: PLC0415

            ha_frontend.add_extra_js_url(hass, EMBED_PATH)
            store["embed"] = EMBED_PATH
            _LOGGER.debug("已注入诊断悬浮窗模块：%s", EMBED_PATH)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning(
                "注入诊断悬浮窗模块失败（设备页「访问」仍可整页打开）", exc_info=True
            )

        try:
            await hass.async_add_executor_job(_read_cached, _PAGE_FILE)
        except OSError:
            _LOGGER.warning("诊断页面文件缺失：%s（请检查 www/ 是否部署完整）", _PAGE_FILE)

    await async_remove_legacy_dashboard(hass)


def _find_device(registry: Any, entry_id: str) -> Any:
    """按 identifier 找设备。

    HA 2026 起 ``async_get_device(identifiers=...)`` 被弃用（identifier 不再跨
    config entry 唯一），改用 ``async_get_device_by_identifier``；这里兼容新旧
    两种 API，避免踩 deprecation（2027.8 会直接失效）。
    """
    identifier = (DOMAIN, entry_id)
    getter = getattr(registry, "async_get_device_by_identifier", None)
    if getter is not None:
        for extra in ({"config_entry_id": entry_id}, {}):
            try:
                device = getter(identifier, **extra)
            except TypeError:
                continue
            if device is not None:
                return device
        return None
    return registry.async_get_device(identifiers={identifier})


async def async_link_device(hass: HomeAssistant, entry_id: str) -> bool:
    """把设备页「访问」指向诊断页面（幂等，地址没变就不写注册表）。"""
    url = page_url(hass)
    if not url:
        _LOGGER.debug("取不到访问地址，暂不写设备链接（可在配置里设置内部地址）")
        return False

    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

    registry = dr.async_get(hass)
    device = _find_device(registry, entry_id)
    if device is None:
        # 设备由实体创建，可能还没建出来
        return False
    if device.configuration_url != url:
        registry.async_update_device(device.id, configuration_url=url)
        _LOGGER.debug("设备「访问」已指向诊断页面：%s", url)
    return True


async def async_remove_legacy_dashboard(hass: HomeAssistant) -> None:
    """删掉旧版本建过的 Lovelace 仪表盘条目与面板（best-effort）。"""
    try:
        from homeassistant.components import frontend as ha_frontend  # noqa: PLC0415
        from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415
        from homeassistant.components.lovelace.dashboard import (  # noqa: PLC0415
            DashboardsCollection,
        )
    except ImportError:
        return

    # 面板表在 hass.data 里，先确认存在再删（HA 对"删不存在的面板"会打 warning）
    panels = None
    for key in (getattr(ha_frontend, "DATA_PANELS", None), "frontend_panels"):
        if not key:
            continue
        value = hass.data.get(key)
        if isinstance(value, dict):
            panels = value
            break
    if panels is not None and LEGACY_DASHBOARD_PATH in panels:
        try:
            ha_frontend.async_remove_panel(hass, LEGACY_DASHBOARD_PATH)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("移除旧诊断面板失败", exc_info=True)

    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        return

    try:
        collection = DashboardsCollection(hass)
        await collection.async_load()
        if collection.data.get(LEGACY_DASHBOARD_PATH) is not None:
            await collection.async_delete_item(LEGACY_DASHBOARD_PATH)
            _LOGGER.info("已移除旧版诊断仪表盘（改用设备页悬浮窗）")
        lovelace_data.dashboards.pop(LEGACY_DASHBOARD_PATH, None)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("删除旧诊断仪表盘条目失败", exc_info=True)
