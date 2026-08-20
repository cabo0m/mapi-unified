from __future__ import annotations

import shutil
import subprocess


def popen_platform_kwargs() -> dict[str, object]:
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= int(subprocess.CREATE_NO_WINDOW)
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= int(subprocess.CREATE_NEW_PROCESS_GROUP)
    return {"creationflags": flags, "start_new_session": False}


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    taskkill = shutil.which("taskkill")
    if taskkill:
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    if process.poll() is None:
        process.kill()
