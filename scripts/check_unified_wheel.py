from __future__ import annotations

import configparser
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MEMBERS = {
    "mapi/cli.py",
    "mapi_core/memory/context_engine.py",
    "mapi_core/memory/self_healing.py",
    "mapi_capabilities/files.py",
    "mapi_capabilities/file_writes.py",
    "mapi_capabilities/git_service.py",
    "mapi_capabilities/git_staging.py",
    "mapi_capabilities/git_commits.py",
    "mapi_capabilities/commands.py",
    "mapi_platform/network.py",
    "mapi_platform/windows/task_scheduler.py",
    "mapi_platform/windows/shell.py",
    "mapi_platform/linux/system_install.py",
    "mapi_platform/linux/shell.py",
}
REQUIRED_ENTRY_POINTS = {
    "mapi",
    "mapi-maintenance",
    "mapi-init",
    "mapi-server",
    "mapi-migrate",
    "mapi-doctor",
    "mapi-recover",
    "mapi-capabilities",
}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        output = Path(td)
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--no-build-isolation", "-w", str(output)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            print(completed.stdout)
            print(completed.stderr, file=sys.stderr)
            return completed.returncode
        wheels = list(output.glob("mapi_agent_memory-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected_one_wheel:{len(wheels)}")
        wheel = wheels[0]
        with ZipFile(wheel) as archive:
            members = set(archive.namelist())
            missing = sorted(REQUIRED_MEMBERS - members)
            entry_name = next((name for name in members if name.endswith(".dist-info/entry_points.txt")), None)
            if entry_name is None:
                raise RuntimeError("entry_points_missing")
            parser = configparser.ConfigParser()
            parser.read_string(archive.read(entry_name).decode("utf-8"))
            scripts = set(parser["console_scripts"]) if parser.has_section("console_scripts") else set()
            missing_scripts = sorted(REQUIRED_ENTRY_POINTS - scripts)
            corpus = "mapi_core/memory/corpora/retrieval_golden_v2.json" in members
        result = {
            "status": "ok" if not missing and not missing_scripts and corpus else "failed",
            "wheel": wheel.name,
            "member_count": len(members),
            "missing_members": missing,
            "missing_entry_points": missing_scripts,
            "retrieval_corpus_packaged": corpus,
        }
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
