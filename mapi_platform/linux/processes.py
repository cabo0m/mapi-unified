from __future__ import annotations

import os
import signal
import subprocess


def popen_platform_kwargs() -> dict[str, object]:
    return {"creationflags": 0, "start_new_session": True}


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if process.poll() is None:
            process.kill()
