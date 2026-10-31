"""配置流：单实例，点一下即可添加。"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class DiagHubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Diag Hub 只允许一个实例。"""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """添加实例。"""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="集成诊断", data={})
