"""listener 归属：谁注册了事件监听器、谁注册了不注销。

做法：临时包装 ``hass.bus.async_listen`` / ``async_listen_once``，
从调用栈认出是哪个集成注册的。**只在开关打开时包装**，关闭时立即还原，
不留任何实例属性。

包装分两级，优先级从高到低：
1. 实例属性（``bus.async_listen = wrapper``）—— 影响面最小；
2. 类属性（``EventBus.async_listen = wrapper``）—— 新版 HA 若给 ``EventBus``
   加了 ``__slots__``，实例赋值会失败，只能退到类级（全局只有一个 EventBus
   实例，效果等价）。类级包装要多一个 self 参数，所以两套 wrapper。
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .activity import _LONG_HEX, _domain_from_path

_LOGGER = logging.getLogger(__name__)

_MAX_DEPTH = 40
# 只包装 async_listen：EventBus.async_listen_once 内部就是调 async_listen，
# 两个都包会同一笔注册计两次。只包底层那个，调用栈依然能认出自定义集成。
_WRAPPED = ("async_listen",)


class ListenerTracker:
    """统计每个集成的监听器存量与累计注册次数。"""

    def __init__(self, hass: HomeAssistant) -> None:
        """Init."""
        self.hass = hass
        self._enabled = False
        # attr -> (安装层级, 原始方法)；层级是 "instance" 或 "class"
        self._orig: dict[str, tuple[str, Any]] = {}
        self._live: dict[str, int] = {}
        self._peak: dict[str, int] = {}
        self._registered: dict[str, int] = {}
        self._by_event: dict[str, int] = {}
        self._patch_error = ""

    @property
    def enabled(self) -> bool:
        """是否已包装。"""
        return self._enabled

    @property
    def patch_error(self) -> str:
        """包装失败的原因（成功时为空串），开关会在界面上暴露它。"""
        return self._patch_error

    # ------------------------------------------------------------------ 开关
    def start(self) -> None:
        """开始统计（幂等）。"""
        if self._enabled:
            return
        self._patch_error = ""
        bus = self.hass.bus
        try:
            for attr in _WRAPPED:
                self._install(bus, attr)
        except Exception as err:  # pylint: disable=broad-except
            self._patch_error = f"{type(err).__name__}: {err}"
            _LOGGER.error("包装事件总线失败：%s", self._patch_error, exc_info=True)
            self._restore()
            return
        self._enabled = True
        _LOGGER.info("Diag Hub listener 统计已启动")

    def stop(self) -> None:
        """还原事件总线并清空统计。"""
        if not self._enabled and not self._orig:
            return
        self._restore()
        self._enabled = False
        self._reset()
        _LOGGER.info("Diag Hub listener 统计已停止")

    # ------------------------------------------------------------------ 安装
    def _install(self, bus: Any, attr: str) -> None:
        """包装一个总线方法：先试实例级，失败退类级。"""
        bound = getattr(bus, attr)
        unbound = getattr(type(bus), attr)

        try:
            setattr(bus, attr, self._make_wrapper(bound))
        except (AttributeError, TypeError):
            # EventBus 用了 __slots__ / 禁止实例属性：退到类级包装
            setattr(type(bus), attr, self._make_class_wrapper(unbound))
            self._orig[attr] = ("class", unbound)
            _LOGGER.debug("事件总线 %s 采用类级包装（实例不允许覆盖）", attr)
            return

        self._orig[attr] = ("instance", bound)

    def _restore(self) -> None:
        """按安装层级还原（幂等）。"""
        bus = self.hass.bus
        for attr, (level, original) in list(self._orig.items()):
            try:
                if level == "class":
                    setattr(type(bus), attr, original)
                elif attr in vars(bus):
                    delattr(bus, attr)
                else:  # 理论上不会走到，兜底写回原方法
                    setattr(bus, attr, original)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("还原 %s 失败", attr, exc_info=True)
        self._orig.clear()

    def _reset(self) -> None:
        """清空统计。"""
        self._live.clear()
        self._peak.clear()
        self._registered.clear()
        self._by_event.clear()

    # ------------------------------------------------------------------ 包装
    def _make_wrapper(self, bound: Callable) -> Callable:
        """实例级包装（不需要 self 参数）。"""

        def wrapper(event_type, callback, *args, **kwargs):
            return self._instrument(
                lambda: bound(event_type, callback, *args, **kwargs), event_type
            )

        return wrapper

    def _make_class_wrapper(self, unbound: Callable) -> Callable:
        """类级包装（第一个参数是 EventBus 实例）。"""

        def wrapper(bus_self, event_type, callback, *args, **kwargs):
            return self._instrument(
                lambda: unbound(bus_self, event_type, callback, *args, **kwargs),
                event_type,
            )

        return wrapper

    def _instrument(self, call: Callable[[], Any], event_type: Any) -> Any:
        """执行原始注册，返回带注销计数的 unsub。"""
        domain = self._caller_domain()
        unsub = call()
        key_event = _LONG_HEX.sub("*", str(event_type))[:40]
        self._bump(domain, key_event, 1)

        def _unsub() -> Any:
            self._bump(domain, key_event, -1)
            return unsub()

        return _unsub

    def _caller_domain(self) -> str:
        """从调用栈找出第一个自定义集成的帧（跳过本文件自己的 wrapper 帧）。"""
        try:
            frame = sys._getframe(1)
        except ValueError:
            return "unknown"
        own = __file__.replace("\\", "/")
        depth = 0
        while frame is not None and depth < _MAX_DEPTH:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if filename != own and (
                "/custom_components/" in filename
                or filename.startswith("custom_components/")
            ):
                return _domain_from_path(filename)
            frame = frame.f_back
            depth += 1
        return "homeassistant"

    def _bump(self, domain: str, event_type: str, delta: int) -> None:
        """维护存量 / 峰值 / 累计。"""
        live = max(0, self._live.get(domain, 0) + delta)
        self._live[domain] = live
        if delta > 0:
            self._registered[domain] = self._registered.get(domain, 0) + 1
            if live > self._peak.get(domain, 0):
                self._peak[domain] = live
        key = f"{domain}|{event_type}"
        val = self._by_event.get(key, 0) + delta
        if val <= 0:
            self._by_event.pop(key, None)
        else:
            self._by_event[key] = val

    # ------------------------------------------------------------------ 读取
    def snapshot(self) -> dict[str, Any]:
        """返回按集成的监听器存量排行。"""
        rows = [
            {
                "domain": domain,
                "live": live,
                "peak": self._peak.get(domain, 0),
                "registered": self._registered.get(domain, 0),
            }
            for domain, live in self._live.items()
            if live or self._registered.get(domain)
        ]
        rows.sort(key=lambda item: (item["live"], item["registered"]), reverse=True)
        by_event = sorted(self._by_event.items(), key=lambda kv: -kv[1])[:30]
        return {
            "enabled": self._enabled,
            "patch_error": self._patch_error,
            "total_live": sum(self._live.values()),
            "integrations": rows[:30],
            "by_event": [{"key": key, "live": live} for key, live in by_event],
        }
