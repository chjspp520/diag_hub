"""HTTP 接口：GET /api/diag_hub?t=TOKEN&a=...

动作：
- status（默认）/ top / tasks  —— 活动归属
- memory  当前存活分配按集成汇总
- snap    建立 tracemalloc 基线
- diff    与基线对比的增量（按集成）
- memstop 关闭 tracemalloc

`requires_auth = False` 便于浏览器直接打开排查，因此只适合内网测试机。
如果放到外网可达的环境，请把 API_TOKEN 填上（见 const.py）。
"""
from __future__ import annotations

import platform
import time
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

try:  # HA 版本号：Config 对象上并没有 version 属性
    from homeassistant.const import __version__ as HA_VERSION
except ImportError:  # pragma: no cover - 极端兜底
    HA_VERSION = "unknown"

from .const import API_TOKEN, DOMAIN, HTTP_TOP_N
from .runtime import DiagHubRuntime

_MEMORY_ACTIONS = ("memory", "snap", "diff", "memstart")

#: platform.platform() 会 open() 解释器可执行文件，属于阻塞调用，只能算一次并缓存
_PLATFORM_LABEL: str | None = None


def _platform_label() -> str:
    """平台描述（阻塞，必须在 executor 里调用）。"""
    global _PLATFORM_LABEL  # noqa: PLW0603
    if _PLATFORM_LABEL is None:
        _PLATFORM_LABEL = platform.platform(terse=True)
    return _PLATFORM_LABEL


def _entry_overview(hass: HomeAssistant) -> dict[str, Any]:
    """配置条目概况：总数 / 已加载 / 重启重试 / 加载失败。

    某些"整体跑不起来"的问题不在实体层面，而在条目本身（setup_error/retry），
    报告里带上这份清单能少问一轮。
    """
    total = 0
    loaded = 0
    not_loaded = 0
    retry: list[str] = []
    failed: list[str] = []
    for entry in hass.config_entries.async_entries():
        total += 1
        state = entry.state
        if state is ConfigEntryState.LOADED:
            loaded += 1
        elif state is ConfigEntryState.SETUP_RETRY:
            retry.append(entry.domain)
        elif state is ConfigEntryState.SETUP_ERROR:
            failed.append(entry.domain)
        else:
            # NOT_LOADED / SETUP_IN_PROGRESS / 迁移中等：一般都正常，计数就够
            not_loaded += 1
    return {
        "total": total,
        "loaded": loaded,
        "not_loaded": not_loaded,
        "setup_retry": sorted(set(retry))[:20],
        "setup_error": sorted(set(failed))[:20],
    }


def _current_runtime(hass: HomeAssistant) -> DiagHubRuntime | None:
    """取当前 entry 的 runtime（重载后也能拿到新的）。"""
    for runtime in (hass.data.get(DOMAIN) or {}).values():
        return runtime  # type: ignore[return-value]
    return None


class DiagHubView(HomeAssistantView):
    """诊断数据出口。"""

    url = f"/api/{DOMAIN}"
    name = f"api:{DOMAIN}"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Store hass."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """返回当前快照。"""
        if API_TOKEN and request.query.get("t", "") != API_TOKEN:
            return web.json_response({"error": "bad token"}, status=403)

        runtime = _current_runtime(self.hass)
        if runtime is None:
            return web.json_response({"error": "diag_hub not loaded"}, status=503)

        # /proc/self/status 也是阻塞读取，先走 executor 刷新缓存（内部有 5 秒节流）
        await runtime.activity.async_refresh_proc()

        action = request.query.get("a", "status")
        data: dict[str, Any]

        if action == "memory":
            data = await self.hass.async_add_executor_job(runtime.memory.usage)
        elif action == "snap":
            data = await self.hass.async_add_executor_job(runtime.memory.snap)
        elif action == "diff":
            data = await self.hass.async_add_executor_job(runtime.memory.diff)
        elif action == "memstart":
            # 免去先去开关页点一下：直接开启 tracing 并立刻算一次分布
            runtime.memory.start()
            data = await self.hass.async_add_executor_job(runtime.memory.usage)
            data["started"] = runtime.memory.enabled
        elif action == "memstop":
            runtime.memory.stop()
            data = {"stopped": True, "enabled": False}
        elif action == "tasks":
            tasks = runtime.activity.snapshot(HTTP_TOP_N).get("tasks", {})
            data = {"action": action, **tasks}
            return web.json_response(data)
        elif action == "listeners":
            data = runtime.listeners.snapshot()
        elif action == "logs":
            data = runtime.logs.snapshot()
        elif action == "all":
            data = {
                # 排查上下文：版本 / 生成时间（页面「复制报告」会带出去）
                "hass_version": HA_VERSION,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "python_version": platform.python_version(),
                "platform": await self.hass.async_add_executor_job(_platform_label),
                "config_dir": self.hass.config.config_dir,
                "entity_count": len(self.hass.states.async_entity_ids()),
                "component_count": len(self.hass.config.components),
                "entries": _entry_overview(self.hass),
                "activity": runtime.activity.snapshot(HTTP_TOP_N),
                "listeners": runtime.listeners.snapshot(),
                "logs": runtime.logs.snapshot(),
                "memory": runtime.memory.last_usage,
            }
        else:
            data = runtime.activity.snapshot(HTTP_TOP_N)
            if action == "top":
                data.pop("tasks", None)

        data["action"] = action
        if action in _MEMORY_ACTIONS or action == "memstop":
            # 内存操作顺手带上上下文，省一次请求
            data["activity_enabled"] = runtime.activity.enabled
            data["tracing"] = runtime.memory.enabled
            data["proc"] = runtime.activity.snapshot(2).get("proc", {})
        return web.json_response(data)
