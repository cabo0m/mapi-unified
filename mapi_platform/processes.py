from __future__ import annotations

from .selector import current_platform

_platform = current_platform()
if _platform == "windows":
    from .windows.processes import popen_platform_kwargs, terminate_process_tree
elif _platform == "linux":
    from .linux.processes import popen_platform_kwargs, terminate_process_tree
else:
    raise RuntimeError(f"unsupported_platform:{_platform}")

__all__ = ["popen_platform_kwargs", "terminate_process_tree"]
