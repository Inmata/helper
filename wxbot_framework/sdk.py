"""插件可使用的公开接口；实现分散在框架内部，这里保持稳定导出。"""
from __future__ import annotations

from .messaging.delivery import LegacyService, ServiceFacade
from .messaging.message import CapabilityError, Message
from .plugin_engine.context import PluginContext

__all__ = ["Message", "PluginContext", "CapabilityError", "ServiceFacade", "LegacyService"]
