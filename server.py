from __future__ import annotations

"""Thin compatibility entrypoint for the private MAPI MAPI runtime."""

import sys
import types
from typing import Any

from app.runtime import admin_tools as _admin_tools
from app.runtime import capability_tools as _capability_tools
from app.runtime import freshness as _freshness
from app.runtime import private_mode as _private_mode
from app.runtime import server_runtime as _runtime
from app.runtime import timeline_tools as _timeline_tools
from app.workshops.runtime_registry import bind_workshop_handlers, validate_workshop_handler_registry

_runtime.install_runtime_overrides()
bind_workshop_handlers(_freshness, replace=True, strict=False, local_only=True)
bind_workshop_handlers(_private_mode, replace=True, strict=False, local_only=True)
bind_workshop_handlers(_admin_tools, replace=True, strict=False, local_only=True)
bind_workshop_handlers(_capability_tools, replace=True, strict=False, local_only=True)
bind_workshop_handlers(_timeline_tools, replace=True, strict=False, local_only=True)
_registry_report = validate_workshop_handler_registry()
if not _registry_report["complete"]:
    raise RuntimeError(f"Incomplete workshop registry: {_registry_report}")

_base = _runtime._base
mcp = _runtime.mcp
ROOT = _runtime.ROOT
DATA_DIR = _runtime.DATA_DIR
DB_PATH = _runtime.DB_PATH


def __getattr__(name: str) -> Any:
    for provider in (_admin_tools, _freshness, _private_mode, _timeline_tools, _runtime, _base):
        if hasattr(provider, name):
            return getattr(provider, name)
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_admin_tools)) | set(dir(_freshness)) | set(dir(_private_mode)) | set(dir(_timeline_tools)) | set(dir(_runtime)) | set(dir(_base)))


class _ServerFacadeModule(types.ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        if name in {"ROOT", "DATA_DIR", "DB_PATH"}:
            setattr(_runtime, name, value)


sys.modules[__name__].__class__ = _ServerFacadeModule


if __name__ == "__main__":
    _runtime.run_server()
