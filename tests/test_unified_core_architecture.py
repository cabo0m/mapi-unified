from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "mapi_core"

FORBIDDEN_IMPORT_PREFIXES = ("mapi_platform", "mapi.")
FORBIDDEN_TEXT_MARKERS = ("systemctl", "/etc/systemd/", "schtasks.exe", "powershell.exe")


def test_core_does_not_depend_on_platform_layers() -> None:
    violations: list[str] = []
    for path in CORE.rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module == "mapi_platform" or module.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{path.relative_to(ROOT)} imports {module}")
        lowered = text.casefold()
        for marker in FORBIDDEN_TEXT_MARKERS:
            if marker.casefold() in lowered:
                violations.append(f"{path.relative_to(ROOT)} contains platform marker {marker}")
    assert violations == []


def test_platform_selector_reports_supported_family() -> None:
    from mapi_platform.selector import current_platform

    assert current_platform() in {"windows", "linux", "unsupported"}
