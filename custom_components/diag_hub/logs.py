"""日志计数：按集成统计 warning / error 条数。

挂一个 root logger 的 Handler，只做 dict 自增，**不保存日志正文**（只留每个集成
最后一条的摘要），因此不会因为日志风暴而吃内存。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_LEVEL_KEY = {
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "critical",
}


def _domain_of_logger(name: str) -> str:
    """logger 名 → 集成域。"""
    if not name:
        return "root"
    if name.startswith("custom_components."):
        parts = name.split(".")
        return parts[1] if len(parts) > 1 else name
    return name.split(".")[0]


class _CounterHandler(logging.Handler):
    """只计数的 handler。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.counts: dict[str, dict[str, int]] = {}
        self.last: dict[str, str] = {}
        self.total = 0

    def emit(self, record: logging.LogRecord) -> None:
        """记录一条（绝不抛异常，绝不做格式化）。"""
        try:
            domain = _domain_of_logger(record.name)
            slot = self.counts.get(domain)
            if slot is None:
                slot = {"warning": 0, "error": 0, "critical": 0}
                self.counts[domain] = slot
            key = _LEVEL_KEY.get(record.levelno)
            if key is None:
                key = "error" if record.levelno >= logging.ERROR else "warning"
            slot[key] += 1
            self.total += 1
            # 必须用 getMessage()：直接用 record.msg 拿到的是未格式化的模板，
            # 带参数的日志会显示成 "State attributes for %s exceed ... of %s bytes"。
            try:
                message = record.getMessage()
            except Exception:  # pylint: disable=broad-except
                message = str(record.msg)
            self.last[domain] = f"{record.levelname} {message[:200]}"
        except Exception:  # pylint: disable=broad-except
            pass


class LogTracker:
    """日志计数开关。"""

    def __init__(self, hass: HomeAssistant) -> None:
        """Init."""
        self.hass = hass
        self._handler: _CounterHandler | None = None

    @property
    def enabled(self) -> bool:
        """是否已挂载 handler。"""
        return self._handler is not None

    def start(self) -> None:
        """挂载 handler（幂等）。"""
        if self._handler is not None:
            return
        handler = _CounterHandler()
        try:
            logging.getLogger().addHandler(handler)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("挂载日志计数器失败")
            return
        self._handler = handler
        _LOGGER.info("Diag Hub 日志计数已启动")

    def stop(self) -> None:
        """移除 handler 并丢弃统计。"""
        if self._handler is None:
            return
        try:
            logging.getLogger().removeHandler(self._handler)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("移除日志计数器失败", exc_info=True)
        self._handler = None
        _LOGGER.info("Diag Hub 日志计数已停止")

    def snapshot(self) -> dict[str, Any]:
        """按集成汇总。"""
        handler = self._handler
        if handler is None:
            return {"enabled": False, "total": 0, "integrations": []}
        rows = [
            {
                "domain": domain,
                "warning": slot["warning"],
                "error": slot["error"],
                "critical": slot["critical"],
                "total": sum(slot.values()),
                "last": handler.last.get(domain, ""),
            }
            for domain, slot in handler.counts.items()
        ]
        rows.sort(key=lambda item: (item["error"] + item["critical"], item["total"]), reverse=True)
        return {
            "enabled": True,
            "total": handler.total,
            "integrations": rows[:30],
        }
